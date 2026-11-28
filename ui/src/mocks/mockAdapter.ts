import { ApiConflictError, ApiReadOnlyError, type CockpitApiAdapter } from '@/api/adapter';
import {
  createInitialMockDataset,
  deterministicHash,
  ORIGIN_ENGINE_VERIFIED,
  ORIGIN_HUMAN_REPO,
  ORIGIN_MCP_DEGRADED,
  type MockDatasetBundle,
} from '@/mocks/fixtures';
import type {
  AskCitation,
  AskRequest,
  AskResponse,
  CloneVersionResponse,
  CreateFactInput,
  CreatePlanInput,
  CreateTaskInput,
  DecideDecisionResponse,
  DependencyGraph,
  DoctorResponse,
  DriftItem,
  DriftMonitorQuery,
  DriftMonitorResponse,
  DropSessionResponse,
  EvidenceClaim,
  ExportSnapshotResponse,
  FileContextResponse,
  GraphEdgeData,
  GraphNodeData,
  PlanSummary,
  PlanTask,
  PromotePlanResponse,
  ReindexResponse,
  ReverifyResponse,
  TestFreshnessItem,
  UiEventType,
  UiSnapshot,
  UiSseEvent,
  UpdatePlanInput,
  VersionDiffField,
  VersionEntityKind,
  VersionHistoryResponse,
} from '@/types/contracts';

type EventListenerFn = (event: UiSseEvent) => void;

class MockEventBus {
  private listeners = new Set<EventListenerFn>();
  private seq = 0;

  subscribe(fn: EventListenerFn): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  publish(type: UiEventType, payload: Record<string, unknown>): UiSseEvent {
    this.seq += 1;
    const envelope: UiSseEvent = {
      id: `mock-ui-${this.seq}`,
      seq: this.seq,
      type,
      timestamp: new Date().toISOString(),
      payload,
    };
    for (const listener of this.listeners) {
      listener(envelope);
    }
    return envelope;
  }
}

export const mockEventBus = new MockEventBus();

function sleepWithAbort(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Request aborted', 'AbortError'));
      return;
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('Request aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function computeDiff(
  before: Record<string, unknown> | undefined,
  after: Record<string, unknown> | undefined,
): VersionDiffField[] {
  const left = before ?? {};
  const right = after ?? {};
  const ignore = new Set(['updated_at', 'created_at', 'file_fingerprints']);
  const keys = Array.from(new Set([...Object.keys(left), ...Object.keys(right)])).sort();
  const diffs: VersionDiffField[] = [];
  for (const key of keys) {
    if (ignore.has(key)) continue;
    const inLeft = key in left;
    const inRight = key in right;
    if (!inLeft && inRight) {
      diffs.push({ field: key, change_type: 'added', before: null, after: structuredClone(right[key]) });
    } else if (inLeft && !inRight) {
      diffs.push({ field: key, change_type: 'removed', before: structuredClone(left[key]), after: null });
    } else if (JSON.stringify(left[key]) !== JSON.stringify(right[key])) {
      diffs.push({
        field: key,
        change_type: 'modified',
        before: structuredClone(left[key]),
        after: structuredClone(right[key]),
      });
    }
  }
  return diffs;
}

/**
 * Full interactive stateful `?mock=1` cockpit adapter & mutation simulator.
 * Works completely standalone in the browser with zero running backend while mirroring
 * `src/cairnmind/ui_server.py` + `ui_cockpit.py` contracts 1:1.
 */
export class MockCockpitAdapter implements CockpitApiAdapter {
  readonly mode = 'mock' as const;
  readonly projectId: string | null;
  private data: MockDatasetBundle;
  private readOnly = false;
  private llmOffline = false;
  private reverifyCount = 0;

  constructor(projectId: string | null = null) {
    this.projectId = projectId;
    this.data = createInitialMockDataset();
  }

  private fileSha(path: string): string {
    return deterministicHash(this.data.files[path] ?? `missing:${path}`);
  }

  private assertWritable(): void {
    if (this.readOnly) {
      throw new ApiReadOnlyError(
        'Cockpit is running in --read-only preview mode; write actions are disabled.',
      );
    }
  }

  private buildGraph(): DependencyGraph {
    const nodes: GraphNodeData[] = [];
    const edges: GraphEdgeData[] = [];
    const seenNodes = new Set<string>();
    const seenEdges = new Set<string>();

    const addNode = (node: GraphNodeData) => {
      if (!seenNodes.has(node.id)) {
        seenNodes.add(node.id);
        nodes.push(node);
      }
    };
    const addEdge = (source: string, target: string, kind: string, status: string) => {
      const id = `${source}->${target}`;
      if (!seenEdges.has(id)) {
        seenEdges.add(id);
        edges.push({ id, source, target, kind, status });
      }
    };

    const fileStatus = new Map<string, string>();
    for (const claim of this.data.claims) {
      if (!claim.source_path) continue;
      const cur = fileStatus.get(claim.source_path) ?? 'VERIFIED';
      if (claim.provenance_status === 'CONFLICT' || claim.drift_kind === 'QUOTE_MUTATED') {
        fileStatus.set(claim.source_path, 'REJECTED');
      } else if (claim.provenance_status === 'STALE' && cur !== 'REJECTED') {
        fileStatus.set(claim.source_path, 'STALE');
      } else if (!fileStatus.has(claim.source_path)) {
        fileStatus.set(claim.source_path, 'VERIFIED');
      }
    }

    for (const path of Object.keys(this.data.files).sort()) {
      const status = fileStatus.get(path) ?? 'VERIFIED';
      const base = path.split('/').pop() ?? path;
      addNode({
        id: `file:${path}`,
        column: 0,
        category: 'source_file',
        label: base,
        subtitle: path,
        status,
        ref_id: path,
        meta: { sha256_short: this.fileSha(path).slice(0, 7) },
      });
    }

    for (const claim of this.data.claims) {
      const evId = `ev:quote:${claim.id}`;
      const evStatus = claim.drift_kind === 'QUOTE_MUTATED' ? 'REJECTED' : claim.provenance_status;
      addNode({
        id: evId,
        column: 1,
        category: 'evidence',
        label: `Quote L${claim.line_start}-${claim.line_end}`,
        subtitle: claim.drift_kind,
        status: evStatus,
        ref_id: claim.id,
        meta: {
          evidence_type: 'quote',
          source_path: claim.source_path,
          sha256_short: claim.recorded_sha256.slice(0, 7),
        },
      });
      if (claim.source_path && seenNodes.has(`file:${claim.source_path}`)) {
        addEdge(`file:${claim.source_path}`, evId, 'anchors_quote', evStatus);
      }

      const claimNodeId = `claim:${claim.id}`;
      addNode({
        id: claimNodeId,
        column: 2,
        category: 'claim',
        label: claim.key,
        subtitle: claim.title,
        status: claim.provenance_status,
        ref_id: claim.id,
        meta: {
          kind: claim.kind,
          version: claim.version,
          origin_category: claim.origin?.category ?? 'ENGINE_VERIFIED',
          origin_evidence: claim.origin?.origin_evidence ?? 'verified',
          origin_client: claim.origin?.origin_client ?? null,
        },
      });
      addEdge(evId, claimNodeId, 'supports_claim', evStatus);

      for (const tid of claim.task_ids) {
        addEdge(claimNodeId, `task:${tid}`, 'informs_task', claim.provenance_status);
      }
    }

    for (const task of this.data.tasks) {
      const gate = task.test_gate;
      const gateStatus =
        gate.status === 'PASS'
          ? 'VERIFIED'
          : gate.status === 'FAIL'
            ? 'REJECTED'
            : 'STALE';
      const taskStatus =
        gate.status === 'FAIL'
          ? 'REJECTED'
          : task.evidence_status === 'STALE' || gate.status === 'STALE_CODE' || gate.status === 'MISSING'
            ? 'STALE'
            : 'VERIFIED';

      const taskNodeId = `task:${task.id}`;
      addNode({
        id: taskNodeId,
        column: 3,
        category: 'task',
        label: `${task.id} · ${task.state}`,
        subtitle: task.title,
        status: taskStatus,
        ref_id: task.id,
        meta: {
          profile: gate.profile_name,
          gate_status: gate.status,
          evidence: `${task.evidence_verified}/${task.evidence_total}`,
          origin_category: task.origin?.category ?? 'ENGINE_VERIFIED',
          origin_evidence: task.origin?.origin_evidence ?? 'verified',
          origin_client: task.origin?.origin_client ?? null,
        },
      });

      if (gate.run_id) {
        const runEvId = `ev:test:${gate.run_id}`;
        addNode({
          id: runEvId,
          column: 1,
          category: 'evidence',
          label: `Test · ${gate.profile_name}`,
          subtitle: `${gate.status} @ ${gate.sha256_short ?? 'none'}`,
          status: gateStatus,
          ref_id: task.id,
          meta: { evidence_type: 'test_run', run_id: gate.run_id },
        });
        for (const srcPath of task.source_paths) {
          if (seenNodes.has(`file:${srcPath}`)) {
            addEdge(`file:${srcPath}`, runEvId, 'hashed_by_test', gateStatus);
          }
        }
        for (const cid of task.claim_ids) {
          addEdge(runEvId, `claim:${cid}`, 'verifies_claim', gateStatus);
        }
      }
    }

    return { nodes, edges };
  }

  async fetchSnapshot(): Promise<UiSnapshot> {
    const fp = deterministicHash(
      Object.keys(this.data.files)
        .sort()
        .map((k) => `${k}:${this.fileSha(k)}`)
        .join('|'),
    );
    const activePlan = this.data.plans.find((p) => p.status === 'active') ?? null;
    const driftedFiles = new Set(
      this.data.claims.filter((c) => c.provenance_status !== 'VERIFIED' && c.source_path).map((c) => c.source_path),
    );
    return structuredClone({
      project: {
        id: this.projectId ?? 'proj-cairnmind-core',
        name: this.projectId ?? 'cairnmind',
        root: '/workspace/cairnmind',
        branch: 'main',
        head: '86a303f9c4e1',
        fingerprint: fp,
        mode: 'mock',
      },
      capabilities: {
        writable: !this.readOnly,
        read_only_reason: this.readOnly ? 'Simulated --read-only preview mode active.' : null,
        ask_available: true,
        llm_configured: !this.llmOffline,
        multi_project: true,
        supported_export_modes: ['team'],
      },
      state_rev: this.data.stateRev,
      updated_at: this.data.updatedAt,
      sessions: this.data.sessions.filter((s) => !s.dropped),
      plans: this.data.plans,
      tasks: this.data.tasks,
      claims: this.data.claims,
      graph: this.buildGraph(),
      audit_log: this.data.auditLog,
      drift_summary: {
        drifted_files_count: driftedFiles.size,
        stale_tests_count: this.data.tasks.filter((t) => t.test_gate.status !== 'PASS').length,
        pending_decisions_count: this.data.detectedDecisions.filter((d) => d.status === 'pending').length,
        last_approved_plan_id: activePlan?.id ?? null,
        last_approved_plan_version: activePlan?.version ?? null,
      },
    });
  }

  async fetchFileContext(path: string, lineStart: number, lineEnd: number): Promise<FileContextResponse> {
    const normalized = path.replace(/\\/g, '/').trim();
    const content = this.data.files[normalized];
    if (!content) {
      throw new Error(`Mock file not found: ${normalized}`);
    }
    const lines = content.split('\n');
    const start = Math.max(1, Math.min(lineStart, lines.length));
    const end = Math.max(start, Math.min(lineEnd, lines.length));
    return {
      path: normalized,
      line_start: start,
      line_end: end,
      total_lines: lines.length,
      current_disk_sha256: this.fileSha(normalized),
      lines: lines.slice(start - 1, end).map((text, idx) => ({ number: start + idx, text })),
    };
  }

  async triggerReverify(): Promise<ReverifyResponse> {
    this.assertWritable();
    const now = new Date().toISOString();
    this.reverifyCount += 1;
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    for (const s of this.data.sessions) {
      if (s.dropped) continue;
      s.current_state_rev = this.data.stateRev;
      if (s.id === 'sess-claude-9f2a') {
        s.acknowledged_rev = this.data.stateRev;
        s.sync_status = 'SYNCED';
        s.last_seen_at = now;
      } else {
        s.sync_status = 'OUTDATED_ACK';
      }
    }
    const staleIds = this.data.claims.filter((c) => c.provenance_status === 'STALE').map((c) => c.id);
    const verifiedCount = this.data.claims.filter((c) => c.provenance_status === 'VERIFIED').length;
    this.data.auditLog.unshift({
      id: `aud-${9007 + this.reverifyCount}`,
      timestamp: now,
      session_id: 'ui-cockpit',
      client: 'cairnmind-ui',
      mcp_tool: 'fingerprints.reverify',
      verdict: 'ACCEPTED',
      reason_code: 'OK',
      message: `Re-verified ${Object.keys(this.data.files).length} source files: ${verifiedCount} verified, ${staleIds.length} stale claims.`,
      target_id: staleIds[0] ?? null,
      arguments: { mode: 'mock', files_scanned: Object.keys(this.data.files).length },
      violation_detail: { rule: 'OK', state_rev: this.data.stateRev, stale_claims: staleIds },
      origin: ORIGIN_HUMAN_REPO('cairnmind-ui', 'human', 'ui-human'),
    });
    if (staleIds.length) {
      mockEventBus.publish('evidence.stale', {
        state_rev: this.data.stateRev,
        claim_ids: staleIds,
        message: `${staleIds.length} claims remain stale after fingerprint scan.`,
      });
    }
    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'reverify',
      checked_files: Object.keys(this.data.files).length,
      stale_claims: staleIds.length,
    });
    return {
      ok: true,
      state_rev: this.data.stateRev,
      checked_files: Object.keys(this.data.files).length,
      stale_claims: staleIds.length,
      verified_claims: verifiedCount,
      timestamp: now,
    };
  }

  async dropSessionLock(sessionId: string): Promise<DropSessionResponse> {
    this.assertWritable();
    const target = this.data.sessions.find((s) => s.id === sessionId && !s.dropped);
    if (!target) throw new Error(`Active session not found: ${sessionId}`);
    const now = new Date().toISOString();
    target.dropped = true;
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'session_dropped',
      session_id: sessionId,
    });
    return {
      ok: true,
      session_id: sessionId,
      dropped: true,
      state_rev: this.data.stateRev,
      timestamp: now,
    };
  }

  async ask(request: AskRequest, signal?: AbortSignal): Promise<AskResponse> {
    const started = performance.now();
    // Realistic brief latency so users can test AbortController / Cancel button interactively.
    await sleepWithAbort(320, signal);

    const query = request.query.trim();
    const qLower = query.toLowerCase();
    const fallbackMode = request.verifier_mode === 'fts_fallback' || this.llmOffline;
    const now = new Date().toISOString();

    const toCitation = (claim: EvidenceClaim): AskCitation => ({
      id: `cit-${claim.id}-${Date.now()}`,
      claim_id: claim.id,
      file_path: claim.source_path,
      line_start: claim.line_start,
      line_end: claim.line_end,
      fingerprint: claim.source_path ? this.fileSha(claim.source_path) : claim.current_disk_sha256,
      verbatim_quote: claim.verbatim_quote,
      origin: claim.origin,
      stale_now: false,
    });

    const isExplicitUnknown =
      /(unknown|billing|stripe|kubernetes|k8s|graphql|payment|oauth|pricing)/.test(qLower);

    if (isExplicitUnknown) {
      return {
        id: `ask-${Date.now().toString(36)}`,
        query,
        status: 'UNKNOWN',
        answer:
          'UNKNOWN — Brak zweryfikowanych dowodów w pamięci projektu ani w zindeksowanym kodzie dla tego pytania. Cairnmind nie generuje odpowiedzi bez dosłownych cytatów źródłowych.',
        citations: [],
        tier_used: fallbackMode ? 'tier_0_fts_evidence' : 'tier_2_verified_llm',
        latency_ms: Math.round(performance.now() - started),
        cached: false,
        fallback_mode: fallbackMode,
        fallback_reason: fallbackMode
          ? 'Brak skonfigurowanego LLM / tryb offline — zapytanie wykonane wyłącznie w trybie Poziom 0 (FTS).'
          : null,
        unknowns: ['Żaden zindeksowany plik ani potwierdzony rekord pamięci nie pokrywa tego tematu.'],
        timestamp: now,
      };
    }

    let matchedClaims: EvidenceClaim[] = [];
    let answer = '';

    if (/(komponent|przepływ|architecture|component|flow|overview)/.test(qLower)) {
      matchedClaims = this.data.claims.filter((c) =>
        ['clm-occ-gate', 'clm-quote-verify', 'clm-plan-cas', 'clm-mcp-firewall'].includes(c.id),
      );
      answer =
        'Główne komponenty Cairnminda tworzą 4-etapowy łańcuch weryfikacji: (1) `SessionManager.require_ack` egzekwuje bramkę OCC (`acknowledged_rev == state_rev`), (2) `verify_verbatim_quote` weryfikuje SHA-256 i obecność dosłownego cytatu w kodzie, (3) `compare_and_swap_task` chroni atomowe wersjonowanie planów i zadań, a (4) `enforce_write_contract` blokuje zapisy MCP bez cytatów lub zaliczonych testów.';
    } else if (/(decyzj|decision|adr|wykryto|recent)/.test(qLower)) {
      matchedClaims = this.data.claims.filter((c) => c.kind === 'decision' || c.kind === 'constraint').slice(0, 3);
      const pendingTitles = this.data.detectedDecisions
        .filter((d) => d.status === 'pending')
        .slice(0, 3)
        .map((d) => d.title)
        .join('; ');
      answer = `Ostatnie zweryfikowane decyzje architektoniczne to rozróżnianie \`FILE_DRIFT_QUOTE_INTACT\` od \`QUOTE_MUTATED\` w \`evidence.py\` oraz podwójny CAS plan+task w \`plans.py\`. Wykryte oczekujące decyzje z repo/PR/CI: ${pendingTitles}.`;
    } else if (/(nieaktualn|stale|drift|blokuj|block)/.test(qLower)) {
      matchedClaims = this.data.claims.filter((c) => c.provenance_status !== 'VERIFIED');
      const staleTasks = this.data.tasks
        .filter((t) => t.test_gate.status !== 'PASS')
        .map((t) => `${t.id} (${t.test_gate.status})`)
        .join(', ');
      answer = `Aktualnie ${matchedClaims.length} dowody są nieaktualne (m.in. \`clm-test-gate-fp\` ze stanem \`QUOTE_MUTATED\` w \`verification.py\` oraz \`clm-occ-gate\` z dryfem hasha w \`sessions.py\`). Powiązane zadania wymagające ponownej weryfikacji: ${staleTasks}.`;
    } else {
      const terms = qLower.match(/[a-z0-9_ąćęłńóśźż]{3,}/gi) ?? [];
      const scored = this.data.claims
        .map((claim) => {
          const hay = `${claim.key} ${claim.title} ${claim.content} ${claim.source_path} ${claim.verbatim_quote}`.toLowerCase();
          const score = terms.reduce((acc, term) => acc + (hay.includes(term) ? 1 : 0), 0);
          return { score, claim };
        })
        .filter((item) => item.score > 0)
        .sort((a, b) => b.score - a.score);

      matchedClaims = scored.slice(0, 3).map((s) => s.claim);
      if (matchedClaims.length === 0) {
        return {
          id: `ask-${Date.now().toString(36)}`,
          query,
          status: 'UNKNOWN',
          answer:
            'UNKNOWN — Nie znaleziono dosłownych dowodów w bazie Cairnminda dla tego pytania. Spróbuj podać nazwę modułu (np. `sessions.py`, `plans.py`, `verification.py`) albo ID zadania.',
          citations: [],
          tier_used: fallbackMode ? 'tier_0_fts_evidence' : 'tier_2_verified_llm',
          latency_ms: Math.round(performance.now() - started),
          cached: false,
          fallback_mode: fallbackMode,
          fallback_reason: fallbackMode
            ? 'Brak LLM / tryb offline — Poziom 0 (FTS) zwrócił 0 dopasowań.'
            : null,
          unknowns: ['Brak pasujących cytatów źródłowych w bieżącym indeksie projektu.'],
          timestamp: now,
        };
      }
      answer = `Na podstawie ${matchedClaims.length} zweryfikowanych rekordów w pamięci projektu (${matchedClaims.map((c) => c.key).join(', ')}): ${matchedClaims[0].content}`;
    }

    const citations = matchedClaims.map(toCitation);
    return {
      id: `ask-${Date.now().toString(36)}`,
      query,
      status: fallbackMode ? 'PARTIAL' : 'VERIFIED',
      answer: fallbackMode
        ? `[Poziom 0 / FTS — bez LLM] Zwrócono ${citations.length} dopasowane dowody źródłowe. ${answer}`
        : answer,
      citations,
      tier_used: fallbackMode ? 'tier_0_fts_evidence' : 'tier_2_verified_llm',
      latency_ms: Math.round(performance.now() - started),
      cached: false,
      fallback_mode: fallbackMode,
      fallback_reason: fallbackMode
        ? 'Brak skonfigurowanego LLM / tryb offline — przełączono automatycznie w tryb wyszukiwania czystych dowodów i cytatów (Poziom 0 / FTS).'
        : null,
      unknowns: [],
      timestamp: now,
    };
  }

  async fetchDriftMonitor(query: DriftMonitorQuery = {}): Promise<DriftMonitorResponse> {
    const approvedPlan = this.data.plans.find((p) => p.status === 'active') ?? null;
    const fp = deterministicHash(
      Object.keys(this.data.files)
        .sort()
        .map((k) => `${k}:${this.fileSha(k)}`)
        .join('|'),
    );

    const driftItems: DriftItem[] = this.data.claims
      .filter((c) => c.provenance_status !== 'VERIFIED')
      .map((c) => ({
        id: `drift-${c.id}`,
        file_path: c.source_path,
        drift_type: c.drift_kind === 'QUOTE_MUTATED' ? 'QUOTE_MUTATED' : 'FILE_MODIFIED',
        actor: c.origin?.category === 'MCP_DEGRADED' ? 'agent' : 'human',
        origin: c.origin ?? ORIGIN_HUMAN_REPO(),
        recorded_sha256: c.recorded_sha256,
        current_sha256: c.current_disk_sha256,
        summary: `${c.key}: ${c.drift_kind} — impacts ${c.task_ids.length} task(s).`,
        impacted_task_ids: [...c.task_ids],
        impacted_claim_ids: [c.id],
        detected_at: c.updated_at,
      }));

    const testFreshness: TestFreshnessItem[] = this.data.tasks.map((task) => {
      const rejected = this.data.auditLog.find(
        (a) => a.target_id === task.id && a.verdict === 'REJECTED',
      );
      let freshness: TestFreshnessItem['freshness'] = 'STALE';
      let reason = `${task.test_gate.status} — code changed after last verified run`;
      if (task.test_gate.status === 'FAIL' || (rejected && task.state === 'blocked')) {
        freshness = 'REJECTED';
        reason = rejected?.reason_code ?? 'TEST_GATE_FAIL';
      } else if (task.test_gate.status === 'PASS' && task.evidence_status === 'VERIFIED') {
        freshness = 'FRESH';
        reason = 'Fingerprint verified against current tree';
      }
      return {
        task_id: task.id,
        task_title: task.title,
        plan_id: task.plan_id,
        profile_name: task.test_gate.profile_name,
        freshness,
        gate_status: task.test_gate.status,
        run_id: task.test_gate.run_id,
        sha256_short: task.test_gate.sha256_short,
        ran_at: task.test_gate.ran_at,
        reason,
        origin: task.origin,
      };
    });

    const allDecisions = this.data.detectedDecisions;
    const directories = Array.from(new Set(allDecisions.map((d) => d.directory))).sort();
    const sourceCounts = {
      local_file: allDecisions.filter((d) => d.source_type === 'local_file').length,
      github: allDecisions.filter((d) => d.source_type === 'github').length,
      ci: allDecisions.filter((d) => d.source_type === 'ci').length,
    };

    let filtered = [...allDecisions];
    if (query.source && query.source !== 'all') {
      filtered = filtered.filter((d) => d.source_type === query.source);
    }
    if (query.directory) {
      filtered = filtered.filter((d) => d.directory.startsWith(query.directory!));
    }
    if (query.status && query.status !== 'all') {
      filtered = filtered.filter((d) => d.status === query.status);
    }

    const pageSize = Math.max(1, Math.min(50, query.page_size ?? 4));
    const total = filtered.length;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const page = Math.max(1, Math.min(query.page ?? 1, totalPages));
    const start = (page - 1) * pageSize;

    return structuredClone({
      approved_plan: approvedPlan,
      repo_state: {
        branch: 'main',
        head: '86a303f9c4e1',
        fingerprint: fp,
        checked_at: this.data.updatedAt,
        dirty_files_count: new Set(driftItems.map((d) => d.file_path)).size,
      },
      drift_items: driftItems,
      test_freshness: testFreshness,
      detected_decisions: {
        items: filtered.slice(start, start + pageSize),
        total,
        page,
        page_size: pageSize,
        total_pages: totalPages,
        directories,
        source_counts: sourceCounts,
      },
    });
  }

  async decideDetectedDecision(
    decisionId: string,
    action: 'accept' | 'ignore',
  ): Promise<DecideDecisionResponse> {
    this.assertWritable();
    const target = this.data.detectedDecisions.find((d) => d.id === decisionId);
    if (!target) throw new Error(`Detected decision not found: ${decisionId}`);
    const now = new Date().toISOString();
    target.status = action === 'accept' ? 'accepted' : 'ignored';
    this.data.stateRev += 1;
    this.data.updatedAt = now;

    let createdClaimId: string | null = null;
    if (action === 'accept') {
      createdClaimId = `clm-${decisionId}`;
      if (!this.data.claims.some((c) => c.id === createdClaimId)) {
        const sha = target.file_path ? this.fileSha(target.file_path) : deterministicHash(target.summary);
        const newClaim: EvidenceClaim = {
          id: createdClaimId,
          kind: target.kind === 'constraint' ? 'constraint' : 'decision',
          key: `detected.${decisionId}`,
          title: target.title,
          content: target.summary,
          status: 'confirmed',
          provenance_status: 'VERIFIED',
          source_path: target.file_path,
          line_start: target.line_start,
          line_end: target.line_end,
          verbatim_quote: target.verbatim_quote,
          recorded_sha256: sha,
          current_disk_sha256: sha,
          quote_survives: true,
          drift_kind: 'MATCH',
          updated_at: now,
          task_ids: [],
          version: 1,
          origin: structuredClone(target.origin),
        };
        this.data.claims.unshift(newClaim);
        this.data.versionHistory[`claim:${createdClaimId}`] = [
          {
            version: 1,
            timestamp: now,
            reason: 'accepted_detected_decision',
            actor_type: 'human',
            actor_label: 'engineer (UI)',
            origin: structuredClone(target.origin),
            audit_entry_id: null,
            summary: `Accepted detected decision from ${target.source_ref}.`,
            snapshot: structuredClone(newClaim) as unknown as Record<string, unknown>,
          },
        ];
      }
    }

    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: `detected_decision_${action}`,
      decision_id: decisionId,
    });
    return {
      ok: true,
      decision: structuredClone(target),
      created_claim_id: createdClaimId,
      state_rev: this.data.stateRev,
    };
  }

  async createPlan(
    input: CreatePlanInput,
  ): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }> {
    this.assertWritable();
    const now = new Date().toISOString();
    const planId = `plan-${input.status}-${Math.random().toString(36).slice(2, 8)}`;
    const linked = Array.from(new Set((input.linked_files ?? []).map((f) => f.trim()).filter(Boolean)));
    const fileFingerprints: Record<string, string> = {};
    for (const path of linked) {
      fileFingerprints[path] = this.fileSha(path);
    }
    if (input.status === 'active') {
      for (const p of this.data.plans) {
        if (p.status === 'active') p.status = 'archived';
      }
    }
    const origin = ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human');
    const plan: PlanSummary = {
      id: planId,
      title: input.title.trim(),
      goal: input.goal.trim(),
      status: input.status,
      version: 1,
      external_ref: input.external_ref?.trim() || null,
      constraints: (input.constraints ?? []).map((c) => c.trim()).filter(Boolean),
      linked_files: linked,
      file_fingerprints: fileFingerprints,
      steps: (input.steps ?? []).map((s) => ({
        key: s.key.trim(),
        title: s.title.trim(),
        depends_on: [...s.depends_on],
        scope: [...s.scope],
        acceptance_criteria: [...s.acceptance_criteria],
      })),
      origin,
      created_at: now,
      updated_at: now,
    };
    this.data.plans.unshift(plan);
    this.data.versionHistory[`plan:${planId}`] = [
      {
        version: 1,
        timestamp: now,
        reason: input.status === 'draft' ? 'draft_created' : 'plan_created',
        actor_type: 'human',
        actor_label: 'engineer (UI)',
        origin: structuredClone(origin),
        audit_entry_id: null,
        summary: `Created ${input.status} plan '${plan.title}'.`,
        snapshot: structuredClone(plan) as unknown as Record<string, unknown>,
      },
    ];
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'plan_created',
      plan_id: planId,
    });
    return { ok: true, plan: structuredClone(plan), state_rev: this.data.stateRev };
  }

  async updatePlan(
    planId: string,
    input: UpdatePlanInput,
  ): Promise<{ ok: boolean; plan: PlanSummary; state_rev: number }> {
    this.assertWritable();
    const target = this.data.plans.find((p) => p.id === planId);
    if (!target) throw new Error(`Plan not found: ${planId}`);

    if (input.expected_version !== target.version) {
      const localProposed = {
        ...structuredClone(target),
        title: input.title?.trim() ?? target.title,
        goal: input.goal?.trim() ?? target.goal,
        external_ref: input.external_ref !== undefined ? input.external_ref : target.external_ref,
        constraints: input.constraints ?? target.constraints,
        linked_files: input.linked_files ?? target.linked_files,
        steps: input.steps ?? target.steps,
      };
      throw new ApiConflictError({
        code: 'stale_plan_version',
        detail: `Plan ${planId} version conflict (409): expected v${input.expected_version}, but server is already at v${target.version}.`,
        entity_kind: 'plan',
        entity_id: planId,
        expected_version: input.expected_version,
        current_version: target.version,
        modified_by: structuredClone(target.origin ?? null),
        server_snapshot: structuredClone(target),
        diff: computeDiff(
          localProposed as unknown as Record<string, unknown>,
          target as unknown as Record<string, unknown>,
        ),
      });
    }

    const now = new Date().toISOString();
    if (input.title !== undefined) target.title = input.title.trim();
    if (input.goal !== undefined) target.goal = input.goal.trim();
    if (input.external_ref !== undefined) target.external_ref = input.external_ref?.trim() || null;
    if (input.constraints !== undefined) target.constraints = input.constraints.map((c) => c.trim()).filter(Boolean);
    if (input.linked_files !== undefined) {
      target.linked_files = Array.from(new Set(input.linked_files.map((f) => f.trim()).filter(Boolean)));
      target.file_fingerprints = Object.fromEntries(target.linked_files.map((p) => [p, this.fileSha(p)]));
    }
    if (input.steps !== undefined) target.steps = structuredClone(input.steps);
    if (input.status !== undefined) target.status = input.status;
    target.version += 1;
    target.updated_at = now;
    target.origin = ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human');

    const hist = this.data.versionHistory[`plan:${planId}`] ?? [];
    hist.push({
      version: target.version,
      timestamp: now,
      reason: 'draft_updated',
      actor_type: 'human',
      actor_label: 'engineer (UI)',
      origin: structuredClone(target.origin),
      audit_entry_id: null,
      summary: `Updated '${target.title}' to v${target.version}.`,
      snapshot: structuredClone(target) as unknown as Record<string, unknown>,
    });
    this.data.versionHistory[`plan:${planId}`] = hist;
    this.data.stateRev += 1;
    this.data.updatedAt = now;

    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'plan_updated',
      plan_id: planId,
      version: target.version,
    });
    return { ok: true, plan: structuredClone(target), state_rev: this.data.stateRev };
  }

  async promotePlan(
    planId: string,
    expectedVersion: number,
    force = false,
  ): Promise<PromotePlanResponse> {
    this.assertWritable();
    const target = this.data.plans.find((p) => p.id === planId);
    if (!target) throw new Error(`Plan not found: ${planId}`);

    if (expectedVersion !== target.version) {
      throw new ApiConflictError({
        code: 'stale_plan_version',
        detail: `Plan ${planId} changed concurrently: expected v${expectedVersion}, current is v${target.version}.`,
        entity_kind: 'plan',
        entity_id: planId,
        expected_version: expectedVersion,
        current_version: target.version,
        modified_by: structuredClone(target.origin ?? null),
        server_snapshot: structuredClone(target),
        diff: [],
      });
    }

    const missingFiles: string[] = [];
    const driftedFiles: Array<{ path: string; recorded_sha256: string; current_sha256: string }> = [];
    const verifiedFiles: string[] = [];
    const recordedFps = target.file_fingerprints ?? {};
    for (const path of target.linked_files) {
      if (!(path in this.data.files)) {
        missingFiles.push(path);
      } else {
        const currentSha = this.fileSha(path);
        const recSha = recordedFps[path];
        if (recSha && recSha !== currentSha) {
          driftedFiles.push({ path, recorded_sha256: recSha, current_sha256: currentSha });
        } else {
          verifiedFiles.push(path);
        }
      }
    }

    const precheck = { missing_files: missingFiles, drifted_files: driftedFiles, verified_files: verifiedFiles };
    if ((missingFiles.length > 0 || driftedFiles.length > 0) && !force) {
      return {
        ok: false,
        requires_confirmation: true,
        precheck,
        plan: structuredClone(target),
        state_rev: this.data.stateRev,
      };
    }

    const now = new Date().toISOString();
    for (const p of this.data.plans) {
      if (p.id !== planId && p.status === 'active') p.status = 'archived';
    }
    target.status = 'active';
    target.version += 1;
    target.updated_at = now;
    target.origin = ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human');
    target.file_fingerprints = Object.fromEntries(
      target.linked_files.filter((p) => p in this.data.files).map((p) => [p, this.fileSha(p)]),
    );

    target.steps.forEach((step, idx) => {
      const exists = this.data.tasks.some((t) => t.plan_id === planId && t.step_key === step.key);
      if (!exists) {
        this.data.tasks.unshift({
          id: `TSK-${200 + this.data.tasks.length + idx + 1}`,
          step_key: step.key,
          title: step.title,
          plan_id: planId,
          plan_title: target.title,
          status: 'confirmed',
          state: 'todo',
          version: 1,
          evidence_verified: 0,
          evidence_total: Math.max(1, step.scope.length),
          evidence_status: 'UNVERIFIED',
          test_gate: { profile_name: 'default', status: 'MISSING', sha256_short: null, run_id: null, ran_at: null },
          updated_at: now,
          claim_ids: [],
          source_paths: [...step.scope],
          depends_on: [...step.depends_on],
          acceptance_criteria: [...step.acceptance_criteria],
          origin: structuredClone(target.origin),
          external_ref: target.external_ref ?? null,
        });
      }
    });

    const hist = this.data.versionHistory[`plan:${planId}`] ?? [];
    hist.push({
      version: target.version,
      timestamp: now,
      reason: 'promoted_active',
      actor_type: 'human',
      actor_label: 'engineer (UI)',
      origin: structuredClone(target.origin),
      audit_entry_id: null,
      summary: `Promoted draft '${target.title}' to Active Plan v${target.version}.`,
      snapshot: structuredClone(target) as unknown as Record<string, unknown>,
    });
    this.data.versionHistory[`plan:${planId}`] = hist;
    this.data.stateRev += 1;
    this.data.updatedAt = now;

    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'plan_promoted',
      plan_id: planId,
      version: target.version,
    });
    return {
      ok: true,
      requires_confirmation: false,
      precheck,
      plan: structuredClone(target),
      state_rev: this.data.stateRev,
    };
  }

  async createTask(
    input: CreateTaskInput,
  ): Promise<{ ok: boolean; task: PlanTask; state_rev: number }> {
    this.assertWritable();
    const targetPlan = input.plan_id ? this.data.plans.find((p) => p.id === input.plan_id) : null;
    if (input.plan_id && !targetPlan) throw new Error(`Plan not found: ${input.plan_id}`);
    if (targetPlan && input.expected_version !== targetPlan.version) {
      throw new ApiConflictError({
        code: 'stale_plan_version',
        detail: `Plan ${targetPlan.id} version conflict: expected v${input.expected_version}, current is v${targetPlan.version}.`,
        entity_kind: 'plan',
        entity_id: targetPlan.id,
        expected_version: input.expected_version,
        current_version: targetPlan.version,
        modified_by: structuredClone(targetPlan.origin ?? null),
        server_snapshot: structuredClone(targetPlan),
        diff: [],
      });
    }

    const now = new Date().toISOString();
    const taskId = `TSK-${this.data.tasks.length + 101}`;
    const stepKey =
      input.step_key?.trim() ||
      input.title
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '')
        .slice(0, 48) ||
      taskId.toLowerCase();
    const origin = ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human');
    const task: PlanTask = {
      id: taskId,
      step_key: stepKey,
      title: input.title.trim(),
      plan_id: targetPlan?.id ?? 'standalone-drafts',
      plan_title: targetPlan?.title ?? 'Standalone Quick Tasks',
      status: input.status ?? 'draft',
      state: (input.status ?? 'draft') === 'draft' ? 'draft' : 'todo',
      version: 1,
      evidence_verified: 0,
      evidence_total: Math.max(1, input.scope?.length ?? 0),
      evidence_status: 'UNVERIFIED',
      test_gate: { profile_name: 'default', status: 'MISSING', sha256_short: null, run_id: null, ran_at: null },
      updated_at: now,
      claim_ids: [],
      source_paths: (input.scope ?? []).map((s) => s.trim()).filter(Boolean),
      depends_on: [],
      acceptance_criteria: (input.acceptance_criteria ?? []).map((s) => s.trim()).filter(Boolean),
      origin,
      external_ref: input.external_ref?.trim() || null,
    };
    this.data.tasks.unshift(task);
    if (targetPlan) {
      targetPlan.version += 1;
      targetPlan.steps.push({
        key: stepKey,
        title: task.title,
        depends_on: [],
        scope: [...task.source_paths],
        acceptance_criteria: [...task.acceptance_criteria],
      });
      targetPlan.updated_at = now;
    }
    this.data.versionHistory[`task:${taskId}`] = [
      {
        version: 1,
        timestamp: now,
        reason: 'quick_task_created',
        actor_type: 'human',
        actor_label: 'engineer (UI)',
        origin: structuredClone(origin),
        audit_entry_id: null,
        summary: `Added standalone quick task '${task.title}'.`,
        snapshot: structuredClone(task) as unknown as Record<string, unknown>,
      },
    ];
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'task_created',
      task_id: taskId,
    });
    return { ok: true, task: structuredClone(task), state_rev: this.data.stateRev };
  }

  async createFact(
    input: CreateFactInput,
  ): Promise<{ ok: boolean; claim: EvidenceClaim; state_rev: number }> {
    this.assertWritable();
    const now = new Date().toISOString();
    const claimId = `clm-${Math.random().toString(36).slice(2, 8)}`;
    const path = (input.source_path ?? '').trim();
    const sha = path && path in this.data.files ? this.fileSha(path) : deterministicHash(input.content);
    const quote = (input.verbatim_quote ?? '').trim();
    const quoteSurvives = path && path in this.data.files && quote ? this.data.files[path].includes(quote) : null;
    const origin = ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human');

    const claim: EvidenceClaim = {
      id: claimId,
      kind: input.kind,
      key: input.key.trim(),
      title: input.title.trim(),
      content: input.content.trim(),
      status: 'confirmed',
      provenance_status: quoteSurvives === false ? 'STALE' : 'VERIFIED',
      source_path: path,
      line_start: input.line_start ?? 1,
      line_end: Math.max(input.line_start ?? 1, input.line_end ?? 20),
      verbatim_quote: quote,
      recorded_sha256: sha,
      current_disk_sha256: sha,
      quote_survives: quoteSurvives,
      drift_kind: quoteSurvives === false ? 'QUOTE_MUTATED' : 'MATCH',
      updated_at: now,
      task_ids: [],
      version: 1,
      origin,
    };
    this.data.claims.unshift(claim);
    this.data.versionHistory[`claim:${claimId}`] = [
      {
        version: 1,
        timestamp: now,
        reason: 'fact_created',
        actor_type: 'human',
        actor_label: 'engineer (UI)',
        origin: structuredClone(origin),
        audit_entry_id: null,
        summary: `Recorded ${input.kind} '${claim.key}'.`,
        snapshot: structuredClone(claim) as unknown as Record<string, unknown>,
      },
    ];
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'fact_created',
      claim_id: claimId,
    });
    return { ok: true, claim: structuredClone(claim), state_rev: this.data.stateRev };
  }

  async fetchVersionHistory(
    entityKind: VersionEntityKind,
    entityId: string,
    fromVersion?: number,
    toVersion?: number,
  ): Promise<VersionHistoryResponse> {
    const key = `${entityKind}:${entityId}`;
    let versions = structuredClone(this.data.versionHistory[key] ?? []);
    if (versions.length === 0) {
      const entity =
        entityKind === 'plan'
          ? this.data.plans.find((p) => p.id === entityId)
          : entityKind === 'task'
            ? this.data.tasks.find((t) => t.id === entityId)
            : this.data.claims.find((c) => c.id === entityId);
      if (!entity) throw new Error(`${entityKind} not found: ${entityId}`);
      const origin = entity.origin ?? ORIGIN_ENGINE_VERIFIED();
      versions = [
        {
          version: entity.version,
          timestamp: entity.updated_at,
          reason: 'current_state',
          actor_type: origin.category === 'HUMAN_REPO' ? 'human' : 'agent',
          actor_label: origin.source_label ?? 'cairnmind',
          origin: structuredClone(origin),
          audit_entry_id: null,
          summary: 'Current recorded snapshot.',
          snapshot: structuredClone(entity) as unknown as Record<string, unknown>,
        },
      ];
    }

    versions.sort((a, b) => a.version - b.version);
    const latest = versions[versions.length - 1].version;
    const toV = toVersion ?? latest;
    const fromV = fromVersion ?? (versions.length >= 2 ? versions[versions.length - 2].version : latest);
    const fromEntry = versions.find((v) => v.version === fromV) ?? versions[0];
    const toEntry = versions.find((v) => v.version === toV) ?? versions[versions.length - 1];

    return {
      entity_kind: entityKind,
      entity_id: entityId,
      current_version: latest,
      from_version: fromEntry.version,
      to_version: toEntry.version,
      versions,
      diff: computeDiff(fromEntry.snapshot, toEntry.snapshot),
    };
  }

  async cloneVersionAsDraft(
    entityKind: VersionEntityKind,
    entityId: string,
    sourceVersion: number,
    titleSuffix?: string,
  ): Promise<CloneVersionResponse> {
    this.assertWritable();
    const history = await this.fetchVersionHistory(entityKind, entityId);
    const chosen = history.versions.find((v) => v.version === sourceVersion);
    if (!chosen) throw new Error(`Version v${sourceVersion} not found`);

    const now = new Date().toISOString();
    const suffix = titleSuffix || `(restored from v${sourceVersion})`;
    const origin = ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human');

    if (entityKind === 'plan') {
      const snap = structuredClone(chosen.snapshot) as unknown as PlanSummary;
      const newId = `plan-draft-clone-${Math.random().toString(36).slice(2, 8)}`;
      const draftPlan: PlanSummary = {
        ...snap,
        id: newId,
        title: `${snap.title ?? entityId} ${suffix}`,
        status: 'draft',
        version: 1,
        origin,
        created_at: now,
        updated_at: now,
      };
      this.data.plans.unshift(draftPlan);
      this.data.versionHistory[`plan:${newId}`] = [
        {
          version: 1,
          timestamp: now,
          reason: `cloned_from_v${sourceVersion}`,
          actor_type: 'human',
          actor_label: 'engineer (UI)',
          origin: structuredClone(origin),
          audit_entry_id: null,
          summary: `Safe rollback draft cloned from ${entityId} v${sourceVersion}.`,
          snapshot: structuredClone(draftPlan) as unknown as Record<string, unknown>,
        },
      ];
      this.data.stateRev += 1;
      this.data.updatedAt = now;
      mockEventBus.publish('state.updated', { state_rev: this.data.stateRev, reason: 'version_cloned_draft', plan_id: newId });
      return { ok: true, source_version: sourceVersion, draft_plan: structuredClone(draftPlan), state_rev: this.data.stateRev };
    }

    if (entityKind === 'claim') {
      const snap = structuredClone(chosen.snapshot) as unknown as EvidenceClaim;
      const newId = `clm-draft-${Math.random().toString(36).slice(2, 8)}`;
      const draftClaim: EvidenceClaim = {
        ...snap,
        id: newId,
        key: `${snap.key ?? entityId}.v${sourceVersion}-draft`,
        title: `${snap.title ?? entityId} ${suffix}`,
        status: 'proposed',
        version: 1,
        origin,
        updated_at: now,
      };
      this.data.claims.unshift(draftClaim);
      this.data.stateRev += 1;
      this.data.updatedAt = now;
      mockEventBus.publish('state.updated', { state_rev: this.data.stateRev, reason: 'version_cloned_draft', claim_id: newId });
      return { ok: true, source_version: sourceVersion, draft_claim: structuredClone(draftClaim), state_rev: this.data.stateRev };
    }

    const snap = structuredClone(chosen.snapshot) as unknown as PlanTask;
    const newId = `TSK-D${this.data.tasks.length + 101}`;
    const draftTask: PlanTask = {
      ...snap,
      id: newId,
      title: `${snap.title ?? entityId} ${suffix}`,
      status: 'draft',
      state: 'draft',
      version: 1,
      origin,
      updated_at: now,
    };
    this.data.tasks.unshift(draftTask);
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    mockEventBus.publish('state.updated', { state_rev: this.data.stateRev, reason: 'version_cloned_draft', task_id: newId });
    return { ok: true, source_version: sourceVersion, draft_task: structuredClone(draftTask), state_rev: this.data.stateRev };
  }

  async triggerReindex(): Promise<ReindexResponse> {
    this.assertWritable();
    const jobId = `reidx-${Math.random().toString(36).slice(2, 7)}`;
    const started = performance.now();
    const stages: Array<[number, string, string]> = [
      [18, 'scanning', 'Scanning repository worktree files'],
      [52, 'hashing', 'Computing SHA-256 fingerprints & checking quote anchors'],
      [84, 'indexing', 'Updating symbol graph & dependency edges'],
      [100, 'complete', 'Repository re-index completed'],
    ];
    for (const [percent, stage, message] of stages) {
      mockEventBus.publish('reindex.progress', { job_id: jobId, percent, stage, message });
      await sleepWithAbort(180);
    }
    const now = new Date().toISOString();
    this.data.stateRev += 1;
    this.data.updatedAt = now;
    mockEventBus.publish('state.updated', {
      state_rev: this.data.stateRev,
      reason: 'reindex_complete',
      files_scanned: Object.keys(this.data.files).length,
    });
    return {
      ok: true,
      job_id: jobId,
      files_scanned: Object.keys(this.data.files).length,
      changed_files: 0,
      state_rev: this.data.stateRev,
      duration_ms: Math.round(performance.now() - started),
      timestamp: now,
    };
  }

  async fetchDoctor(): Promise<DoctorResponse> {
    const staleClaims = this.data.claims.filter((c) => c.provenance_status !== 'VERIFIED').length;
    const fp = deterministicHash(
      Object.keys(this.data.files)
        .sort()
        .map((k) => `${k}:${this.fileSha(k)}`)
        .join('|'),
    );
    const warnings: string[] = [];
    if (staleClaims > 0) warnings.push(`${staleClaims} claim(s) have stale file fingerprints or mutated quotes.`);
    if (this.llmOffline) warnings.push('LLM offline/disabled — Ask runs in Tier 0 FTS verbatim evidence mode.');
    if (this.readOnly) warnings.push('Running in simulated --read-only preview mode.');

    return {
      status: warnings.length > 0 ? 'WARNING' : 'HEALTHY',
      backend_mode: 'mock',
      writable: !this.readOnly,
      read_only_reason: this.readOnly ? 'Simulated --read-only preview mode active.' : null,
      llm_status: {
        configured: !this.llmOffline,
        mode: this.llmOffline ? 'disabled' : 'enabled',
        chat_model: this.llmOffline ? null : 'qwen2.5-coder:14b',
        embedding_model: this.llmOffline ? null : 'nomic-embed-text',
        fallback: 'tier_0_fts_evidence',
      },
      index_freshness: {
        status: staleClaims > 0 ? 'STALE' : 'FRESH',
        files_indexed: Object.keys(this.data.files).length,
        stale_claims: staleClaims,
        last_indexed_at: this.data.updatedAt,
        worktree_fingerprint: fp.slice(0, 16),
      },
      scaling_thresholds: [
        { metric: 'Active Records', current: this.data.claims.length + this.data.tasks.length, threshold: 10000, status: 'OK', unit: 'rows' },
        { metric: 'Indexed Files', current: Object.keys(this.data.files).length, threshold: 25000, status: 'OK', unit: 'files' },
        { metric: 'Graph Nodes', current: this.buildGraph().nodes.length, threshold: 50000, status: 'OK', unit: 'nodes' },
        {
          metric: 'Stale Claims Ratio',
          current: Math.round((staleClaims / Math.max(1, this.data.claims.length)) * 100),
          threshold: 25,
          status: staleClaims > 0 ? 'WARN' : 'OK',
          unit: '%',
        },
      ],
      warnings,
      checked_at: new Date().toISOString(),
    };
  }

  async exportTeamSnapshot(preview = false): Promise<ExportSnapshotResponse> {
    if (!preview) this.assertWritable();
    const now = new Date().toISOString();
    const confirmed = this.data.claims.filter(
      (c) => c.status === 'confirmed' && c.origin?.origin_evidence !== 'degraded',
    );
    const activePlans = this.data.plans.filter((p) => p.status === 'active');
    const payloadPreview = {
      schema_version: 2,
      mode: 'team',
      project: 'cairnmind',
      exported_at: now,
      plans: activePlans.map((p) => ({ id: p.id, title: p.title, version: p.version, goal: p.goal })),
      records: confirmed.map((c) => ({
        id: c.id,
        kind: c.kind,
        key: c.key,
        content: c.content,
        source_path: c.source_path,
        recorded_sha256: c.recorded_sha256,
      })),
    };
    const digest = deterministicHash(JSON.stringify(payloadPreview));
    return {
      ok: true,
      mode: 'team',
      preview,
      redaction_gate: {
        status: 'PASS',
        secrets_scanned: 4,
        secrets_redacted: 0,
        findings: [],
      },
      summary: {
        confirmed_records: confirmed.length,
        plans_included: activePlans.length,
        held_proposed: this.data.claims.filter((c) => c.status !== 'confirmed').length,
        held_degraded: this.data.claims.filter((c) => c.origin?.origin_evidence === 'degraded').length,
        excluded_transcripts: this.data.sessions.length,
      },
      artifact_filename: `cairnmind-team-snapshot-${digest.slice(0, 8)}.json`,
      snapshot_sha256: digest,
      exported_at: now,
      payload_preview: payloadPreview,
    };
  }

  readonly mockControls = {
    simulateConflict: async (planId: string, client = 'codex-cli') => {
      const target = this.data.plans.find((p) => p.id === planId);
      if (!target) throw new Error(`Plan not found: ${planId}`);
      const now = new Date().toISOString();
      target.version += 1;
      target.updated_at = now;
      target.goal = `${target.goal.replace(/\.$/, '')} [concurrent MCP edit by ${client}].`;
      target.origin =
        client === 'external'
          ? ORIGIN_MCP_DEGRADED('external')
          : ORIGIN_ENGINE_VERIFIED(client);
      const hist = this.data.versionHistory[`plan:${planId}`] ?? [];
      hist.push({
        version: target.version,
        timestamp: now,
        reason: 'mcp_concurrent_edit',
        actor_type: 'agent',
        actor_label: client,
        origin: structuredClone(target.origin),
        audit_entry_id: 'aud-9007',
        summary: `Concurrent MCP save by ${client} bumped plan to v${target.version}.`,
        snapshot: structuredClone(target) as unknown as Record<string, unknown>,
      });
      this.data.versionHistory[`plan:${planId}`] = hist;
      this.data.stateRev += 1;
      this.data.updatedAt = now;
      mockEventBus.publish('plan.conflict', {
        plan_id: planId,
        current_version: target.version,
        client,
        message: `${client} updated ${planId} to v${target.version} via MCP while editor was open.`,
      });
      mockEventBus.publish('state.updated', {
        state_rev: this.data.stateRev,
        reason: 'mcp_concurrent_edit',
        plan_id: planId,
      });
      return { current_version: target.version, state_rev: this.data.stateRev };
    },

    simulateFileEdit: async (filePath: string) => {
      const normalized = filePath.replace(/\\/g, '/').trim();
      if (!(normalized in this.data.files)) {
        throw new Error(`Mock file not found: ${normalized}`);
      }
      const now = new Date().toISOString();
      const oldSha = this.fileSha(normalized);
      this.data.files[normalized] = `${this.data.files[normalized]}\n# live IDE edit ${now}\n`;
      const newSha = this.fileSha(normalized);
      const impacted: string[] = [];
      for (const claim of this.data.claims) {
        if (claim.source_path === normalized) {
          claim.current_disk_sha256 = newSha;
          claim.provenance_status = 'STALE';
          if (claim.drift_kind === 'MATCH') claim.drift_kind = 'FILE_DRIFT_QUOTE_INTACT';
          claim.updated_at = now;
          impacted.push(claim.id);
        }
      }
      this.data.stateRev += 1;
      this.data.updatedAt = now;
      mockEventBus.publish('repo_fingerprint_changed', {
        file_path: normalized,
        old_sha256: oldSha,
        new_sha256: newSha,
        claim_ids: impacted,
        state_rev: this.data.stateRev,
        message: `IDE modified ${normalized} -> sha256:${newSha.slice(0, 7)}`,
      });
      if (impacted.length > 0) {
        mockEventBus.publish('evidence.stale', {
          state_rev: this.data.stateRev,
          claim_ids: impacted,
          file_path: normalized,
          message: `${impacted.length} claim(s) became stale after live file edit.`,
        });
      }
      return { file_path: normalized, new_sha256: newSha, claim_ids: impacted };
    },

    setReadOnly: (readOnly: boolean) => {
      this.readOnly = readOnly;
      mockEventBus.publish('state.updated', {
        state_rev: this.data.stateRev,
        reason: readOnly ? 'read_only_enabled' : 'read_only_disabled',
      });
    },

    setLlmOffline: (offline: boolean) => {
      this.llmOffline = offline;
      mockEventBus.publish('state.updated', {
        state_rev: this.data.stateRev,
        reason: offline ? 'llm_offline_simulated' : 'llm_online_simulated',
      });
    },

    getMockFlags: () => ({
      readOnly: this.readOnly,
      llmOffline: this.llmOffline,
    }),
  };
}
