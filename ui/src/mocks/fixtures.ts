import type {
  AgentSession,
  DetectedDecision,
  EvidenceClaim,
  GuardrailAuditEntry,
  OriginMetadata,
  PlanSummary,
  PlanTask,
  VersionEntry,
} from '@/types/contracts';

/** Deterministic lightweight 64-hex fingerprint generator for in-browser mock mode. */
export function deterministicHash(input: string): string {
  let h1 = 0xdeadbeef ^ input.length;
  let h2 = 0x41c6ce57 ^ input.length;
  let h3 = 0x9e3779b9 ^ input.length;
  let h4 = 0x85ebca6b ^ input.length;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
    h3 = Math.imul(h3 ^ (ch + i), 2246822519);
    h4 = Math.imul(h4 ^ (ch * 31), 3266489917);
  }
  const part = (n: number) => (n >>> 0).toString(16).padStart(8, '0');
  const half = part(h1) + part(h2) + part(h3) + part(h4);
  return (half + part(h2 ^ h3) + part(h1 ^ h4) + part(h3 ^ h1) + part(h4 ^ h2)).slice(0, 64);
}

export const ORIGIN_ENGINE_VERIFIED = (client = 'cairnmind-engine'): OriginMetadata => ({
  category: 'ENGINE_VERIFIED',
  origin_evidence: 'verified',
  origin_client: client,
  source_label: client,
  detail: 'Directly verified by Cairnmind engine against immutable file hashes and verbatim quotes.',
});

export const ORIGIN_HUMAN_REPO = (
  sourceLabel = 'human-ui',
  evidence: 'human' | 'repo_ci' = 'human',
  client = 'ui-human',
): OriginMetadata => ({
  category: 'HUMAN_REPO',
  origin_evidence: evidence,
  origin_client: client,
  source_label: sourceLabel,
  detail:
    evidence === 'human'
      ? 'Authored directly by an engineer in the Cairnmind UI cockpit.'
      : 'Detected automatically from repository ADRs, GitHub PRs, or CI checks.',
});

export const ORIGIN_MCP_DEGRADED = (client = 'external'): OriginMetadata => ({
  category: 'MCP_DEGRADED',
  origin_evidence: 'degraded',
  origin_client: client,
  source_label: `${client} (degraded)`,
  detail:
    'origin_evidence: degraded — written by an external MCP agent whose conversation history is unarchived and not directly verified by the engine.',
});

export const INITIAL_MOCK_FILES: Record<string, string> = {
  'src/cairnmind/sessions.py': `"""Deterministic, versioned resume packets and OCC write gates."""
from __future__ import annotations

import hashlib
from typing import Any

PACKET_SCHEMA_VERSION = 2


class SessionManager:
    def require_ack(self, session_id: str, packet_id: str | None = None) -> None:
        """Reject writes unless the session acknowledged the latest state_rev."""
        session = self._session(session_id)
        current_rev = self.current_state_rev(session["project_id"])
        acknowledged_rev = session.get("acknowledged_rev", 0)
        if acknowledged_rev < current_rev:
            raise ValueError(
                f"OCC_STALE_RESUME_PACKET: session {session_id} ack_rev={acknowledged_rev} "
                f"< state_rev={current_rev}; fetch resume_context and ack_resume first."
            )
        if packet_id and session.get("active_packet_id") != packet_id:
            raise ValueError("OCC_STALE_RESUME_PACKET: packet_id no longer active.")
`,
  'src/cairnmind/evidence.py': `"""Derived verbatim quote provenance and file fingerprint checks."""
from __future__ import annotations

import hashlib
from pathlib import Path


def verify_verbatim_quote(file_path: Path, recorded_sha256: str, verbatim_quote: str) -> dict:
    """Compare on-disk SHA-256 and exact verbatim passage against recorded provenance."""
    raw = file_path.read_bytes()
    disk_sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace").replace("\\r\\n", "\\n")
    quote_intact = verbatim_quote in text
    if disk_sha256 == recorded_sha256 and quote_intact:
        return {"status": "VERIFIED", "drift_kind": "MATCH", "disk_sha256": disk_sha256}
    if quote_intact:
        return {"status": "STALE", "drift_kind": "FILE_DRIFT_QUOTE_INTACT", "disk_sha256": disk_sha256}
    return {"status": "STALE", "drift_kind": "QUOTE_MUTATED", "disk_sha256": disk_sha256}
`,
  'src/cairnmind/verification.py': `"""CLI-only test gate execution bound to the Git working tree fingerprint."""
from __future__ import annotations

import hashlib
from pathlib import Path


def evaluate_test_gate(profile_name: str, expected_fingerprint: str, current_fingerprint: str) -> dict:
    """A passing test run stays valid only while the worktree fingerprint is unchanged."""
    if expected_fingerprint != current_fingerprint:
        return {
            "profile_name": profile_name,
            "status": "STALE_CODE",
            "reason": "FILE_FINGERPRINT_MISMATCH",
        }
    return {
        "profile_name": profile_name,
        "status": "PASS",
        "sha256_short": current_fingerprint[:7],
    }
`,
  'src/cairnmind/plans.py': `"""Atomic, versioned project plans shared by independent coding clients."""
from __future__ import annotations


def compare_and_swap_task(task: dict, expected_version: int, expected_plan_version: int) -> dict:
    """Reject concurrent progress writes if either task or plan version diverged."""
    if task["version"] != expected_version:
        raise ValueError("Task version conflict; read the current plan and retry.")
    if task["plan_version"] != expected_plan_version:
        raise ValueError("Plan version conflict; read the current plan and retry.")
    return {**task, "version": task["version"] + 1}
`,
  'src/cairnmind/mcp_server.py': `"""Stdio MCP guardrail firewall forwarding validated tool writes to local service."""
from __future__ import annotations


def enforce_write_contract(tool_name: str, args: dict, session_state: dict) -> None:
    """Block hallucinated quotes, stale OCC packets, or missing test proofs."""
    if not session_state.get("synced"):
        raise PermissionError("OCC_STALE_RESUME_PACKET")
    if tool_name == "remember" and not args.get("verbatim_quote"):
        raise ValueError("VERBATIM_QUOTE_NOT_FOUND")
    if tool_name == "task_done" and not session_state.get("test_gate_passed"):
        raise ValueError("TEST_GATE_UNVERIFIED")
`,
};

export interface MockDatasetBundle {
  files: Record<string, string>;
  stateRev: number;
  updatedAt: string;
  sessions: AgentSession[];
  claims: EvidenceClaim[];
  tasks: PlanTask[];
  plans: PlanSummary[];
  auditLog: GuardrailAuditEntry[];
  detectedDecisions: DetectedDecision[];
  versionHistory: Record<string, VersionEntry[]>;
}

export function createInitialMockDataset(): MockDatasetBundle {
  const files: Record<string, string> = { ...INITIAL_MOCK_FILES };
  const shaSessions = deterministicHash(files['src/cairnmind/sessions.py']);
  const shaEvidence = deterministicHash(files['src/cairnmind/evidence.py']);
  const shaVerification = deterministicHash(files['src/cairnmind/verification.py']);
  const shaPlans = deterministicHash(files['src/cairnmind/plans.py']);
  const shaMcp = deterministicHash(files['src/cairnmind/mcp_server.py']);

  const oldShaSessions = deterministicHash(files['src/cairnmind/sessions.py'] + '\n# rev-39');
  const oldShaVerification = deterministicHash(files['src/cairnmind/verification.py'] + '\n# rev-37');
  const oldShaMcp = deterministicHash(files['src/cairnmind/mcp_server.py'] + '\n# rev-40');

  const sessions: AgentSession[] = [
    {
      id: 'sess-claude-9f2a',
      client: 'claude-code',
      worktree: '/workspace/cairnmind',
      acknowledged_rev: 42,
      current_state_rev: 42,
      sync_status: 'SYNCED',
      packet_id: 'pkt-0042-a91f',
      packet_hash: shaSessions.slice(0, 16),
      last_seen_at: '2026-11-26T12:30:11.204Z',
      degraded: false,
      dropped: false,
    },
    {
      id: 'sess-codex-3c81',
      client: 'codex-cli',
      worktree: '/workspace/cairnmind',
      acknowledged_rev: 39,
      current_state_rev: 42,
      sync_status: 'OUTDATED_ACK',
      packet_id: 'pkt-0039-d402',
      packet_hash: oldShaSessions.slice(0, 16),
      last_seen_at: '2026-11-26T12:26:48.910Z',
      degraded: false,
      dropped: false,
    },
    {
      id: 'sess-cursor-7d04',
      client: 'external',
      worktree: '/workspace/cairnmind',
      acknowledged_rev: 38,
      current_state_rev: 42,
      sync_status: 'OUTDATED_ACK',
      packet_id: 'pkt-0038-11be',
      packet_hash: oldShaVerification.slice(0, 16),
      last_seen_at: '2026-11-26T12:19:03.044Z',
      degraded: true,
      dropped: false,
    },
  ];

  const claims: EvidenceClaim[] = [
    {
      id: 'clm-occ-gate',
      kind: 'constraint',
      key: 'sessions.require_ack.occ_guard',
      title: 'OCC write gate rejects sessions with acknowledged_rev < state_rev',
      content:
        'Every shared-memory write must pass SessionManager.require_ack; outdated resume packets raise OCC_STALE_RESUME_PACKET.',
      status: 'confirmed',
      provenance_status: 'STALE',
      source_path: 'src/cairnmind/sessions.py',
      line_start: 11,
      line_end: 22,
      verbatim_quote: 'if acknowledged_rev < current_rev:\n            raise ValueError(',
      recorded_sha256: oldShaSessions,
      current_disk_sha256: shaSessions,
      quote_survives: true,
      drift_kind: 'FILE_DRIFT_QUOTE_INTACT',
      updated_at: '2026-11-26T12:24:10.000Z',
      task_ids: ['TSK-101', 'TSK-102'],
      version: 3,
      origin: ORIGIN_ENGINE_VERIFIED('cairnmind-engine'),
    },
    {
      id: 'clm-quote-verify',
      kind: 'decision',
      key: 'evidence.verbatim_quote_check',
      title: 'Provenance verification distinguishes hash drift from quote mutation',
      content:
        'verify_verbatim_quote separates FILE_DRIFT_QUOTE_INTACT from QUOTE_MUTATED using raw UTF-8 substring matching.',
      status: 'confirmed',
      provenance_status: 'VERIFIED',
      source_path: 'src/cairnmind/evidence.py',
      line_start: 8,
      line_end: 18,
      verbatim_quote:
        'if disk_sha256 == recorded_sha256 and quote_intact:\n        return {"status": "VERIFIED", "drift_kind": "MATCH", "disk_sha256": disk_sha256}',
      recorded_sha256: shaEvidence,
      current_disk_sha256: shaEvidence,
      quote_survives: true,
      drift_kind: 'MATCH',
      updated_at: '2026-11-26T12:29:02.115Z',
      task_ids: ['TSK-103'],
      version: 2,
      origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
    },
    {
      id: 'clm-test-gate-fp',
      kind: 'constraint',
      key: 'verification.fingerprint_binding',
      title: 'Test gate passes invalidate immediately on worktree fingerprint mismatch',
      content:
        'evaluate_test_gate binds passing runs to the exact Git manifest fingerprint and emits STALE_CODE on drift.',
      status: 'confirmed',
      provenance_status: 'STALE',
      source_path: 'src/cairnmind/verification.py',
      line_start: 8,
      line_end: 19,
      verbatim_quote: 'return {"profile": profile_name, "status": "LEGACY_OK"}',
      recorded_sha256: oldShaVerification,
      current_disk_sha256: shaVerification,
      quote_survives: false,
      drift_kind: 'QUOTE_MUTATED',
      updated_at: '2026-11-26T12:15:40.700Z',
      task_ids: ['TSK-102', 'TSK-104'],
      version: 4,
      origin: ORIGIN_MCP_DEGRADED('cursor'),
    },
    {
      id: 'clm-plan-cas',
      kind: 'decision',
      key: 'plans.compare_and_swap',
      title: 'Plan progress writes require both task and plan CAS versions',
      content:
        'compare_and_swap_task rejects stale task versions and plan versions atomically before incrementing.',
      status: 'confirmed',
      provenance_status: 'VERIFIED',
      source_path: 'src/cairnmind/plans.py',
      line_start: 5,
      line_end: 12,
      verbatim_quote:
        'if task["plan_version"] != expected_plan_version:\n        raise ValueError("Plan version conflict; read the current plan and retry.")',
      recorded_sha256: shaPlans,
      current_disk_sha256: shaPlans,
      quote_survives: true,
      drift_kind: 'MATCH',
      updated_at: '2026-11-26T12:28:51.004Z',
      task_ids: ['TSK-101'],
      version: 1,
      origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
    },
    {
      id: 'clm-mcp-firewall',
      kind: 'fact',
      key: 'mcp_server.write_firewall',
      title: 'MCP write firewall requires verbatim quote and passing test gate',
      content:
        'enforce_write_contract rejects unverified remember and task_done calls before store mutation.',
      status: 'proposed',
      provenance_status: 'STALE',
      source_path: 'src/cairnmind/mcp_server.py',
      line_start: 5,
      line_end: 13,
      verbatim_quote:
        'if tool_name == "task_done" and not session_state.get("test_gate_passed"):\n        raise ValueError("TEST_GATE_UNVERIFIED")',
      recorded_sha256: oldShaMcp,
      current_disk_sha256: shaMcp,
      quote_survives: true,
      drift_kind: 'FILE_DRIFT_QUOTE_INTACT',
      updated_at: '2026-11-26T12:21:17.390Z',
      task_ids: ['TSK-104', 'TSK-105'],
      version: 2,
      origin: ORIGIN_MCP_DEGRADED('external'),
    },
  ];

  const tasks: PlanTask[] = [
    {
      id: 'TSK-101',
      step_key: 'occ-session-gate',
      title: 'Enforce OCC resume_packet check across concurrent MCP writers',
      plan_id: 'plan-release-012',
      plan_title: 'v0.12 Concurrency & Provenance Hardening',
      status: 'confirmed',
      state: 'in_progress',
      version: 5,
      evidence_verified: 1,
      evidence_total: 2,
      evidence_status: 'STALE',
      test_gate: {
        profile_name: 'occ-concurrency',
        status: 'STALE_CODE',
        sha256_short: oldShaSessions.slice(0, 7),
        run_id: 'chk-8812',
        ran_at: '2026-11-26T12:18:04.112Z',
      },
      updated_at: '2026-11-26T12:28:51.004Z',
      claim_ids: ['clm-occ-gate', 'clm-plan-cas'],
      source_paths: ['src/cairnmind/sessions.py', 'src/cairnmind/plans.py'],
      depends_on: [],
      acceptance_criteria: [
        'Reject writes when acknowledged_rev < state_rev',
        'Preserve atomic rollback on plan CAS conflict',
      ],
      origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
      external_ref: 'REL-012',
    },
    {
      id: 'TSK-102',
      step_key: 'fingerprint-reverify',
      title: 'Invalidate test gates when tracked worktree SHA-256 drifts',
      plan_id: 'plan-release-012',
      plan_title: 'v0.12 Concurrency & Provenance Hardening',
      status: 'confirmed',
      state: 'needs_test',
      version: 4,
      evidence_verified: 0,
      evidence_total: 2,
      evidence_status: 'STALE',
      test_gate: {
        profile_name: 'verification-gate',
        status: 'STALE_CODE',
        sha256_short: oldShaVerification.slice(0, 7),
        run_id: 'chk-8809',
        ran_at: '2026-11-26T12:14:22.880Z',
      },
      updated_at: '2026-11-26T12:24:10.000Z',
      claim_ids: ['clm-occ-gate', 'clm-test-gate-fp'],
      source_paths: ['src/cairnmind/sessions.py', 'src/cairnmind/verification.py'],
      depends_on: ['TSK-101'],
      acceptance_criteria: [
        'Mark test gate STALE_CODE on any manifest hash drift',
        'Detect mutated verbatim quotes in replaced files',
      ],
      origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
      external_ref: null,
    },
    {
      id: 'TSK-103',
      step_key: 'quote-provenance-viewer',
      title: 'Render highlighted verbatim quote provenance with SHA-256 diff',
      plan_id: 'plan-release-012',
      plan_title: 'v0.12 Concurrency & Provenance Hardening',
      status: 'confirmed',
      state: 'done',
      version: 3,
      evidence_verified: 1,
      evidence_total: 1,
      evidence_status: 'VERIFIED',
      test_gate: {
        profile_name: 'provenance-unit',
        status: 'PASS',
        sha256_short: shaEvidence.slice(0, 7),
        run_id: 'chk-8840',
        ran_at: '2026-11-26T12:29:18.501Z',
      },
      updated_at: '2026-11-26T12:29:18.501Z',
      claim_ids: ['clm-quote-verify'],
      source_paths: ['src/cairnmind/evidence.py'],
      depends_on: [],
      acceptance_criteria: [
        'Compare recorded_sha256 against current_disk_sha256',
        'Highlight exact verbatim_quote substring in file snippet',
      ],
      origin: ORIGIN_ENGINE_VERIFIED('cairnmind-engine'),
      external_ref: null,
    },
    {
      id: 'TSK-104',
      step_key: 'mcp-guardrail-firewall',
      title: 'Block unverified MCP task completions and hallucinated quotes',
      plan_id: 'plan-release-012',
      plan_title: 'v0.12 Concurrency & Provenance Hardening',
      status: 'confirmed',
      state: 'blocked',
      version: 2,
      evidence_verified: 0,
      evidence_total: 2,
      evidence_status: 'STALE',
      test_gate: {
        profile_name: 'mcp-guardrails',
        status: 'FAIL',
        sha256_short: shaMcp.slice(0, 7),
        run_id: 'chk-8835',
        ran_at: '2026-11-26T12:22:01.003Z',
      },
      updated_at: '2026-11-26T12:22:01.003Z',
      claim_ids: ['clm-test-gate-fp', 'clm-mcp-firewall'],
      source_paths: ['src/cairnmind/verification.py', 'src/cairnmind/mcp_server.py'],
      depends_on: ['TSK-102'],
      acceptance_criteria: [
        'Reject remember without exact verbatim quote in cited file',
        'Reject task_done when required test gate is not PASS',
      ],
      origin: ORIGIN_MCP_DEGRADED('cursor'),
      external_ref: null,
    },
    {
      id: 'TSK-105',
      step_key: 'blast-radius-inspector',
      title: 'Illuminate downstream blast radius from edited source files',
      plan_id: 'plan-release-012',
      plan_title: 'v0.12 Concurrency & Provenance Hardening',
      status: 'proposed',
      state: 'todo',
      version: 1,
      evidence_verified: 0,
      evidence_total: 1,
      evidence_status: 'STALE',
      test_gate: {
        profile_name: 'ui-smoke',
        status: 'MISSING',
        sha256_short: null,
        run_id: null,
        ran_at: null,
      },
      updated_at: '2026-11-26T12:21:17.390Z',
      claim_ids: ['clm-mcp-firewall'],
      source_paths: ['src/cairnmind/mcp_server.py'],
      depends_on: ['TSK-103', 'TSK-104'],
      acceptance_criteria: [
        'Render deterministic 4-column dagre graph',
        'Highlight downstream evidence, claims, and tasks on click',
      ],
      origin: ORIGIN_HUMAN_REPO('github-pr#464', 'repo_ci', 'github'),
      external_ref: 'PR-464',
    },
  ];

  const plans: PlanSummary[] = [
    {
      id: 'plan-release-012',
      title: 'v0.12 Concurrency & Provenance Hardening',
      goal: 'Guarantee deterministic OCC resume gates, verbatim quote provenance, and worktree-bound test gates.',
      status: 'active',
      version: 3,
      external_ref: 'REL-012',
      constraints: [
        'Every shared-memory write requires acknowledged_rev == state_rev',
        'Never mutate confirmed verdicts from external degraded MCP sessions',
      ],
      linked_files: [
        'src/cairnmind/sessions.py',
        'src/cairnmind/evidence.py',
        'src/cairnmind/verification.py',
        'src/cairnmind/plans.py',
        'src/cairnmind/mcp_server.py',
      ],
      file_fingerprints: {
        'src/cairnmind/sessions.py': shaSessions,
        'src/cairnmind/evidence.py': shaEvidence,
        'src/cairnmind/verification.py': shaVerification,
        'src/cairnmind/plans.py': shaPlans,
        'src/cairnmind/mcp_server.py': shaMcp,
      },
      steps: tasks.map((t) => ({
        key: t.step_key,
        title: t.title,
        depends_on: [...t.depends_on],
        scope: [...t.source_paths],
        acceptance_criteria: [...t.acceptance_criteria],
      })),
      origin: ORIGIN_ENGINE_VERIFIED('cairnmind-engine'),
      created_at: '2026-11-25T09:10:00.000Z',
      updated_at: '2026-11-26T12:28:51.004Z',
    },
    {
      id: 'plan-draft-jira-1234',
      title: 'Multi-project Rust Adapter & CAS Conflict Recovery',
      goal: 'Prepare project_id-scoped API routes and interactive 409 conflict recovery without breaking existing Inspector workflows.',
      status: 'draft',
      version: 2,
      external_ref: 'JIRA-1234',
      constraints: [
        'Zero visual regression in Obsidian Precision Glass aesthetic',
        'All mutations require expected_version CAS token',
      ],
      linked_files: ['src/cairnmind/plans.py', 'src/cairnmind/sessions.py'],
      file_fingerprints: {
        'src/cairnmind/plans.py': shaPlans,
        'src/cairnmind/sessions.py': shaSessions,
      },
      steps: [
        {
          key: 'adapter-project-prefix',
          title: 'Introduce optional project_id prefix in UI HTTP adapter',
          depends_on: [],
          scope: ['src/cairnmind/plans.py'],
          acceptance_criteria: ['Routes accept /api/projects/{project_id}/*'],
        },
        {
          key: 'cas-409-recovery',
          title: 'Render glass 409 conflict diff banner with refresh or fork draft actions',
          depends_on: ['adapter-project-prefix'],
          scope: ['src/cairnmind/sessions.py'],
          acceptance_criteria: ['Never overwrite remote edits silently'],
        },
      ],
      origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
      created_at: '2026-11-26T10:00:00.000Z',
      updated_at: '2026-11-26T12:20:00.000Z',
    },
    {
      id: 'plan-draft-stale-paths',
      title: 'Legacy Verifier Cleanup (Pre-check Demo)',
      goal: 'Demonstrate fingerprint pre-check warning when promoting a draft referencing deleted or modified paths.',
      status: 'draft',
      version: 1,
      external_ref: 'JIRA-1299',
      constraints: ['Verify every referenced path exists before promoting to active'],
      linked_files: ['src/cairnmind/verification.py', 'src/cairnmind/legacy_verifier.py'],
      file_fingerprints: {
        'src/cairnmind/verification.py': deterministicHash('stale-baseline-verification'),
        'src/cairnmind/legacy_verifier.py': deterministicHash('deleted-file'),
      },
      steps: [
        {
          key: 'retire-legacy-verifier',
          title: 'Retire obsolete verification fallback helper',
          depends_on: [],
          scope: ['src/cairnmind/verification.py'],
          acceptance_criteria: ['Pre-check warns on deleted and drifted files before promotion'],
        },
      ],
      origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
      created_at: '2026-11-26T11:15:00.000Z',
      updated_at: '2026-11-26T11:15:00.000Z',
    },
  ];

  const auditLog: GuardrailAuditEntry[] = [
    {
      id: 'aud-9007',
      timestamp: '2026-11-26T12:30:14.412Z',
      session_id: 'sess-codex-3c81',
      client: 'codex-cli',
      mcp_tool: 'plans.save',
      verdict: 'REJECTED',
      reason_code: 'OCC_STALE_RESUME_PACKET',
      message: 'Session acknowledged_rev #39 is behind current state_rev #42.',
      target_id: 'TSK-101',
      arguments: {
        project: 'cairnmind-core',
        session_id: 'sess-codex-3c81',
        packet_id: 'pkt-0039-d402',
        task_id: 'TSK-101',
        expected_version: 4,
        state: 'done',
      },
      violation_detail: {
        rule: 'OCC_STALE_RESUME_PACKET',
        acknowledged_rev: 39,
        current_state_rev: 42,
        active_packet_id: 'pkt-0042-a91f',
        remediation: 'Call resume_context, inspect state_rev #42 changes, then call ack_resume before retrying.',
      },
      origin: ORIGIN_ENGINE_VERIFIED('codex-cli'),
    },
    {
      id: 'aud-9006',
      timestamp: '2026-11-26T12:29:41.087Z',
      session_id: 'sess-cursor-7d04',
      client: 'external',
      mcp_tool: 'memory.remember',
      verdict: 'REJECTED',
      reason_code: 'VERBATIM_QUOTE_NOT_FOUND',
      message: 'Quoted snippet does not exist verbatim in src/cairnmind/verification.py.',
      target_id: 'clm-test-gate-fp',
      arguments: {
        project: 'cairnmind-core',
        session_id: 'sess-cursor-7d04',
        kind: 'constraint',
        key: 'verification.fingerprint_binding',
        source_path: 'src/cairnmind/verification.py',
        verbatim_quote: 'return {"profile": profile_name, "status": "LEGACY_OK"}',
      },
      violation_detail: {
        rule: 'VERBATIM_QUOTE_NOT_FOUND',
        source_path: 'src/cairnmind/verification.py',
        disk_sha256: shaVerification,
        reason: 'Verbatim passage missing from current file revision; hallucinated or deleted quote blocked.',
      },
      origin: ORIGIN_MCP_DEGRADED('external'),
    },
    {
      id: 'aud-9005',
      timestamp: '2026-11-26T12:29:18.501Z',
      session_id: 'sess-claude-9f2a',
      client: 'claude-code',
      mcp_tool: 'memory.remember',
      verdict: 'ACCEPTED',
      reason_code: 'OK',
      message: 'Verified verbatim quote and matching SHA-256 anchored clm-quote-verify.',
      target_id: 'clm-quote-verify',
      arguments: {
        project: 'cairnmind-core',
        session_id: 'sess-claude-9f2a',
        packet_id: 'pkt-0042-a91f',
        kind: 'decision',
        key: 'evidence.verbatim_quote_check',
        source_path: 'src/cairnmind/evidence.py',
      },
      violation_detail: {
        rule: 'OK',
        recorded_sha256: shaEvidence,
        state_rev: 42,
      },
      origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
    },
    {
      id: 'aud-9004',
      timestamp: '2026-11-26T12:27:05.622Z',
      session_id: 'sess-claude-9f2a',
      client: 'claude-code',
      mcp_tool: 'task.complete',
      verdict: 'REJECTED',
      reason_code: 'TEST_GATE_UNVERIFIED',
      message: "Task TSK-104 requires passing check profile 'mcp-guardrails' on current fingerprint.",
      target_id: 'TSK-104',
      arguments: {
        project: 'cairnmind-core',
        session_id: 'sess-claude-9f2a',
        packet_id: 'pkt-0042-a91f',
        task_id: 'TSK-104',
        state: 'done',
      },
      violation_detail: {
        rule: 'TEST_GATE_UNVERIFIED',
        required_profile: 'mcp-guardrails',
        last_run_id: 'chk-8835',
        last_outcome: 'FAIL',
      },
      origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
    },
    {
      id: 'aud-9003',
      timestamp: '2026-11-26T12:25:19.104Z',
      session_id: 'sess-codex-3c81',
      client: 'codex-cli',
      mcp_tool: 'checks.attach',
      verdict: 'REJECTED',
      reason_code: 'FILE_FINGERPRINT_MISMATCH',
      message: 'Check run chk-8809 fingerprint differs from current worktree manifest.',
      target_id: 'TSK-102',
      arguments: {
        project: 'cairnmind-core',
        session_id: 'sess-codex-3c81',
        task_id: 'TSK-102',
        check_run_id: 'chk-8809',
      },
      violation_detail: {
        rule: 'FILE_FINGERPRINT_MISMATCH',
        check_fingerprint: oldShaVerification.slice(0, 16),
        current_fingerprint: shaVerification.slice(0, 16),
        drifted_files: ['src/cairnmind/verification.py', 'src/cairnmind/sessions.py'],
      },
      origin: ORIGIN_ENGINE_VERIFIED('codex-cli'),
    },
    {
      id: 'aud-9002',
      timestamp: '2026-11-26T12:23:44.918Z',
      session_id: 'ui-human',
      client: 'cairnmind-ui',
      mcp_tool: 'plans.save_draft',
      verdict: 'ACCEPTED',
      reason_code: 'OK',
      message: 'Engineer saved draft JIRA-1234 v2 with CAS expected_version=1.',
      target_id: 'plan-draft-jira-1234',
      arguments: {
        plan_id: 'plan-draft-jira-1234',
        expected_version: 1,
        status: 'draft',
      },
      violation_detail: {
        rule: 'OK',
        new_version: 2,
      },
      origin: ORIGIN_HUMAN_REPO('cairnmind-ui', 'human', 'ui-human'),
    },
  ];

  const detectedDecisions: DetectedDecision[] = [
    {
      id: 'det-adr-001',
      title: 'ADR-014: Symbol-anchored evidence survives line shifts',
      summary:
        'Bind extracted decisions to AST symbol body hashes in addition to raw file SHA-256 so formatting edits avoid false staleness.',
      kind: 'decision',
      source_type: 'local_file',
      source_ref: 'docs/adr/014-symbol-anchors.md',
      directory: 'docs/adr/',
      file_path: 'src/cairnmind/evidence.py',
      line_start: 8,
      line_end: 18,
      verbatim_quote: 'def verify_verbatim_quote(file_path: Path, recorded_sha256: str, verbatim_quote: str) -> dict:',
      status: 'pending',
      detected_at: '2026-11-26T12:25:00.000Z',
      origin: ORIGIN_HUMAN_REPO('adr-scanner', 'repo_ci', 'adr-scanner'),
    },
    {
      id: 'det-pr-465',
      title: 'PR #465: Opt-in code decision ingestion defaults to off',
      summary:
        'Code comments, ADRs, and Git trailers remain disabled per project until explicitly enabled; ingested items start as proposed.',
      kind: 'constraint',
      source_type: 'github',
      source_ref: 'GitHub PR #465',
      directory: 'src/cairnmind/',
      file_path: 'src/cairnmind/mcp_server.py',
      line_start: 5,
      line_end: 13,
      verbatim_quote: 'def enforce_write_contract(tool_name: str, args: dict, session_state: dict) -> None:',
      status: 'pending',
      detected_at: '2026-11-26T12:18:10.000Z',
      origin: ORIGIN_HUMAN_REPO('github-pr#465', 'repo_ci', 'github'),
    },
    {
      id: 'det-ci-918',
      title: 'CI Scan #918: Windows native file-lock verification required',
      summary:
        'CI workflow mandates fast-tier Windows tests alongside Linux/macOS coverage merges (>=95% threshold).',
      kind: 'constraint',
      source_type: 'ci',
      source_ref: 'CI Workflow #918',
      directory: '.github/workflows/',
      file_path: 'src/cairnmind/verification.py',
      line_start: 8,
      line_end: 19,
      verbatim_quote: 'def evaluate_test_gate(profile_name: str, expected_fingerprint: str, current_fingerprint: str) -> dict:',
      status: 'pending',
      detected_at: '2026-11-26T12:10:45.000Z',
      origin: ORIGIN_HUMAN_REPO('ci-workflow#918', 'repo_ci', 'ci-scan'),
    },
    {
      id: 'det-pr-468',
      title: 'PR #468: External history imports always carry origin_evidence=degraded',
      summary:
        'Imported third-party transcripts remain searchable but never anchor verified memory or bypass human review.',
      kind: 'decision',
      source_type: 'github',
      source_ref: 'GitHub PR #468',
      directory: 'src/cairnmind/',
      file_path: 'src/cairnmind/sessions.py',
      line_start: 10,
      line_end: 22,
      verbatim_quote: 'class SessionManager:',
      status: 'pending',
      detected_at: '2026-11-26T11:55:20.000Z',
      origin: ORIGIN_HUMAN_REPO('github-pr#468', 'repo_ci', 'github'),
    },
    {
      id: 'det-adr-022',
      title: 'RFC-022: Read-before-write CAS enforced for human UI edits',
      summary:
        'UI mutations submit expected_version tokens identically to MCP agents so concurrent saves trigger controlled 409 diff resolution.',
      kind: 'plan',
      source_type: 'local_file',
      source_ref: 'docs/adr/022-ui-cas.md',
      directory: 'docs/adr/',
      file_path: 'src/cairnmind/plans.py',
      line_start: 5,
      line_end: 12,
      verbatim_quote: 'def compare_and_swap_task(task: dict, expected_version: int, expected_plan_version: int) -> dict:',
      status: 'pending',
      detected_at: '2026-11-26T11:42:00.000Z',
      origin: ORIGIN_HUMAN_REPO('adr-scanner', 'repo_ci', 'adr-scanner'),
    },
    {
      id: 'det-ci-905',
      title: 'CI Scan #905: Redaction gate blocks unredacted tokens in team exports',
      summary:
        'Every team memory publication must pass secret scanning before creating shareable snapshots.',
      kind: 'constraint',
      source_type: 'ci',
      source_ref: 'CI Workflow #905',
      directory: '.github/workflows/',
      file_path: 'src/cairnmind/evidence.py',
      line_start: 1,
      line_end: 10,
      verbatim_quote: '"""Derived verbatim quote provenance and file fingerprint checks."""',
      status: 'accepted',
      detected_at: '2026-11-26T10:30:00.000Z',
      origin: ORIGIN_HUMAN_REPO('ci-workflow#905', 'repo_ci', 'ci-scan'),
    },
    {
      id: 'det-ui-001',
      title: 'UI Spec: Thin adapter boundary ready for Rust multi-project backend',
      summary:
        'Keep all REST/SSE calls behind ui/src/api/ with optional project_id path prefixes.',
      kind: 'decision',
      source_type: 'local_file',
      source_ref: 'ui/src/api/adapter.ts',
      directory: 'ui/src/',
      file_path: 'src/cairnmind/plans.py',
      line_start: 1,
      line_end: 8,
      verbatim_quote: '"""Atomic, versioned project plans shared by independent coding clients."""',
      status: 'pending',
      detected_at: '2026-11-26T09:50:00.000Z',
      origin: ORIGIN_HUMAN_REPO('code-comment', 'repo_ci', 'code-comment'),
    },
    {
      id: 'det-pr-453',
      title: 'PR #453: Single-file change indexer latency budget under 2s',
      summary:
        'Incremental worktree hashing skips unchanged inodes and retries renamed-back directories safely.',
      kind: 'decision',
      source_type: 'github',
      source_ref: 'GitHub PR #453',
      directory: 'src/cairnmind/',
      file_path: 'src/cairnmind/verification.py',
      line_start: 1,
      line_end: 12,
      verbatim_quote: '"""CLI-only test gate execution bound to the Git working tree fingerprint."""',
      status: 'pending',
      detected_at: '2026-11-26T09:12:00.000Z',
      origin: ORIGIN_HUMAN_REPO('github-pr#453', 'repo_ci', 'github'),
    },
    {
      id: 'det-ui-002',
      title: 'UI Spec: Draft plans never execute or mutate active worktree state',
      summary:
        'Draft plans remain isolated until explicitly promoted with file fingerprint pre-check.',
      kind: 'plan',
      source_type: 'local_file',
      source_ref: 'ui/src/components/drafts/',
      directory: 'ui/src/',
      file_path: 'src/cairnmind/plans.py',
      line_start: 5,
      line_end: 11,
      verbatim_quote: 'if task["version"] != expected_version:',
      status: 'pending',
      detected_at: '2026-11-26T08:45:00.000Z',
      origin: ORIGIN_HUMAN_REPO('code-comment', 'repo_ci', 'code-comment'),
    },
  ];

  const versionHistory: Record<string, VersionEntry[]> = {
    'plan:plan-release-012': [
      {
        version: 1,
        timestamp: '2026-11-25T09:10:00.000Z',
        reason: 'draft_created',
        actor_type: 'human',
        actor_label: 'piotr (UI)',
        origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
        audit_entry_id: null,
        summary: 'Initial release hardening draft with OCC session & CAS steps.',
        snapshot: {
          id: 'plan-release-012',
          title: 'v0.12 Concurrency Hardening',
          goal: 'Introduce OCC session checks.',
          status: 'draft',
          version: 1,
          external_ref: 'REL-012',
          constraints: ['Every shared-memory write requires acknowledged_rev == state_rev'],
          linked_files: ['src/cairnmind/sessions.py', 'src/cairnmind/plans.py'],
          steps: plans[0].steps.slice(0, 2),
        },
      },
      {
        version: 2,
        timestamp: '2026-11-26T10:15:00.000Z',
        reason: 'promoted_active',
        actor_type: 'agent',
        actor_label: 'claude-code',
        origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
        audit_entry_id: 'aud-9005',
        summary: 'Promoted to active and expanded provenance quote & MCP firewall gates.',
        snapshot: {
          id: 'plan-release-012',
          title: 'v0.12 Concurrency & Provenance Hardening',
          goal: 'Guarantee OCC resume gates and verbatim quote provenance.',
          status: 'active',
          version: 2,
          external_ref: 'REL-012',
          constraints: ['Every shared-memory write requires acknowledged_rev == state_rev'],
          linked_files: ['src/cairnmind/sessions.py', 'src/cairnmind/evidence.py', 'src/cairnmind/plans.py'],
          steps: plans[0].steps.slice(0, 4),
        },
      },
      {
        version: 3,
        timestamp: '2026-11-26T12:28:51.004Z',
        reason: 'task_progress',
        actor_type: 'agent',
        actor_label: 'claude-code',
        origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
        audit_entry_id: 'aud-9007',
        summary: 'Added Blast-Radius Inspector task and finalized non-degraded verdict constraint.',
        snapshot: structuredClone(plans[0]) as unknown as Record<string, unknown>,
      },
    ],
    'plan:plan-draft-jira-1234': [
      {
        version: 1,
        timestamp: '2026-11-26T10:00:00.000Z',
        reason: 'draft_created',
        actor_type: 'human',
        actor_label: 'piotr (UI)',
        origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
        audit_entry_id: null,
        summary: 'Created Jira draft JIRA-1234 for Rust adapter layer.',
        snapshot: {
          id: 'plan-draft-jira-1234',
          title: 'Multi-project Rust Adapter',
          goal: 'Prepare project_id-scoped API routes.',
          status: 'draft',
          version: 1,
          external_ref: 'JIRA-1234',
          constraints: ['Zero visual regression in Obsidian Precision Glass aesthetic'],
          linked_files: ['src/cairnmind/plans.py'],
          steps: plans[1].steps.slice(0, 1),
        },
      },
      {
        version: 2,
        timestamp: '2026-11-26T12:20:00.000Z',
        reason: 'draft_updated',
        actor_type: 'human',
        actor_label: 'piotr (UI)',
        origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
        audit_entry_id: 'aud-9002',
        summary: 'Added CAS 409 conflict diff recovery step and sessions.py linkage.',
        snapshot: structuredClone(plans[1]) as unknown as Record<string, unknown>,
      },
    ],
    'claim:clm-occ-gate': [
      {
        version: 1,
        timestamp: '2026-11-25T11:00:00.000Z',
        reason: 'created',
        actor_type: 'agent',
        actor_label: 'cairnmind-engine',
        origin: ORIGIN_ENGINE_VERIFIED('cairnmind-engine'),
        audit_entry_id: null,
        summary: 'Initial extraction of SessionManager.require_ack guard.',
        snapshot: {
          ...claims[0],
          version: 1,
          status: 'proposed',
          provenance_status: 'VERIFIED',
          drift_kind: 'MATCH',
        },
      },
      {
        version: 2,
        timestamp: '2026-11-26T09:00:00.000Z',
        reason: 'human_confirmed',
        actor_type: 'human',
        actor_label: 'piotr (UI)',
        origin: ORIGIN_HUMAN_REPO('human-ui', 'human', 'ui-human'),
        audit_entry_id: 'aud-9005',
        summary: 'Engineer confirmed OCC constraint against sessions.py.',
        snapshot: {
          ...claims[0],
          version: 2,
          status: 'confirmed',
          provenance_status: 'VERIFIED',
          drift_kind: 'MATCH',
        },
      },
      {
        version: 3,
        timestamp: '2026-11-26T12:24:10.000Z',
        reason: 'fingerprint_drift',
        actor_type: 'agent',
        actor_label: 'codex-cli',
        origin: ORIGIN_ENGINE_VERIFIED('codex-cli'),
        audit_entry_id: 'aud-9007',
        summary: 'Recorded SHA-256 drifted after sessions.py edit; quote remains intact.',
        snapshot: structuredClone(claims[0]) as unknown as Record<string, unknown>,
      },
    ],
    'task:TSK-101': [
      {
        version: 4,
        timestamp: '2026-11-26T11:45:00.000Z',
        reason: 'task_started',
        actor_type: 'agent',
        actor_label: 'claude-code',
        origin: ORIGIN_ENGINE_VERIFIED('claude-code'),
        audit_entry_id: null,
        summary: 'Moved task to in_progress after resume packet ACK #41.',
        snapshot: {
          ...tasks[0],
          version: 4,
          evidence_status: 'VERIFIED',
          test_gate: { ...tasks[0].test_gate, status: 'PASS' },
        },
      },
      {
        version: 5,
        timestamp: '2026-11-26T12:28:51.004Z',
        reason: 'code_drift_detected',
        actor_type: 'agent',
        actor_label: 'codex-cli',
        origin: ORIGIN_ENGINE_VERIFIED('codex-cli'),
        audit_entry_id: 'aud-9007',
        summary: 'Test gate transitioned to STALE_CODE; outdated Codex completion rejected.',
        snapshot: structuredClone(tasks[0]) as unknown as Record<string, unknown>,
      },
    ],
  };

  return {
    files,
    stateRev: 42,
    updatedAt: '2026-11-26T12:30:14.412Z',
    sessions,
    claims,
    tasks,
    plans,
    auditLog,
    detectedDecisions,
    versionHistory,
  };
}
