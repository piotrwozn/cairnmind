import { REPO_META } from '@/lib/repoMeta';

export interface FeaturePillar {
  id: string;
  eyebrow: string;
  title: string;
  summary: string;
  stability: 'Stable 1.0' | 'Stable 1.0 + Preview';
  capabilities: {
    name: string;
    detail: string;
    command?: string;
  }[];
  docHref: string;
  docLabel: string;
}

export const FEATURE_PILLARS: FeaturePillar[] = [
  {
    id: 'verified-knowledge',
    eyebrow: '01 · Verified Knowledge & Citation Engine',
    title: 'Verbatim source quotes or an explicit unknown',
    summary:
      'Cairnmind never promotes a model claim into independent evidence. Every answer is checked verbatim against current files and passed through deterministic subject, support, and names verification.',
    stability: 'Stable 1.0 + Preview',
    capabilities: [
      {
        name: 'Verbatim quote & multi-stage support checks',
        detail:
          'Quotes must occur literally in current sources. Subject, support, and product-names checks withhold unsupported claims as partial or unknown instead of fabricating numbers.',
        command: 'cairnmind ask my-app "When do accounts lock?" --json',
      },
      {
        name: 'Deterministic-only mode (zero models required)',
        detail:
          'Runs without Ollama or any LLM: plans, tasks, checks, impact, indexing, and full-text passage search stay operational while ask explicitly reports status: unavailable instead of guessing.',
        command: 'cairnmind status --json',
      },
      {
        name: 'Quote self-healing & symbol-anchored evidence',
        detail:
          'Tracks source lines across refactors using Git diffs, AST symbol context, SHA-256 validation, and CAS audit logs, plus opt-in @decision code-comment ADRs.',
        command: 'cairnmind memory heal-audit my-app --json',
      },
      {
        name: 'Optional NLI / VCR verification cascade (Preview)',
        detail:
          'Optional cairnmind[nli] ONNX verifier layer with verdict caching for local entailment diagnostics alongside the standard support gate.',
        command: 'cairnmind ask my-app "…" --verifier-mode nli',
      },
    ],
    docHref: '/docs/contracts/',
    docLabel: 'Read implementation contracts',
  },
  {
    id: 'handoffs-and-occ',
    eyebrow: '02 · Cold Handoffs, OCC & Universal MCP',
    title: 'Move work cleanly between assistants without silent overwrites',
    summary:
      'Every session reads and acknowledges a checksummed resume packet before writing. Concurrent agents coordinate through monotonic plan versions and optimistic concurrency checks.',
    stability: 'Stable 1.0',
    capabilities: [
      {
        name: 'Checksummed resume packets (v2) & read-before-write ACK',
        detail:
          'Sessions receive a bounded packet with state_hash, origin attribution (unattributed, external, automatic_extraction), and short readback when unchanged. Session writes are refused until ACK matches live state.',
        command: 'cairnmind session show SESSION_ID --json',
      },
      {
        name: 'Versioned plans, stage router & CAS concurrency',
        detail:
          'Atomic plan and step updates enforce expected_version / expected_plan_version so two assistants never silently clobber each other. The stage next command recommends the next assistant or check.',
        command: 'cairnmind plan next my-app --json',
      },
      {
        name: 'First-class & external coding assistants (MCP presets)',
        detail:
          'Launches Codex CLI, Claude Code (including cairnmind-recorder subagent), GitHub Copilot SDK, and built-in API agents (OpenAI, Anthropic, Ollama, OpenAI-compatible). Generates atomic, backed-up MCP configs for popular editors and any MCP client.',
        command: 'cairnmind mcp generate --client claude-code --write',
      },
      {
        name: 'Direct MCP Mode for external editors (transparent provenance)',
        detail:
          'Cursor, Cline, Windsurf, VS Code, Zed, and Gemini CLI connect via cairnmind mcp --client external. Cairnmind does not wrap the IDE process, so writes carry origin_evidence: degraded in JSON and you always see what was engine-verified vs IDE-reported.',
        command: 'cairnmind mcp test-connection',
      },
    ],
    docHref: '/docs/external-clients/',
    docLabel: 'External clients & MCP guide',
  },
  {
    id: 'test-gates-and-ci',
    eyebrow: '03 · Machine-Verified Done & Serverless CI',
    title: 'An agent cannot claim done after code changes',
    summary:
      'Test-gated tasks require fresh local runs of named check profiles bound to the worktree code fingerprint. Edit one line and previous passing runs immediately go stale.',
    stability: 'Stable 1.0',
    capabilities: [
      {
        name: 'Named check profiles (.cairnmind/checks.toml)',
        detail:
          'Executes declared argv directly (no shell, isolated process group, explicit timeouts and output limits) and records exit code + stdout/stderr bound to task definition and code fingerprint.',
        command: 'cairnmind check run my-app unit --task TASK_ID',
      },
      {
        name: 'Memory Pulse micro-audit (< 500 ms)',
        detail:
          'Read-only consistency check inspecting code drift, stale test tasks, degraded entities, and snapshot integrity with exit codes 0 (healthy), 1 (drift with --strict), and 2 (broken) for CI and pre-push hooks.',
        command: 'cairnmind pulse my-app --strict --json',
      },
      {
        name: 'Serverless CI scan & blast-radius impact',
        detail:
          'Run cairnmind scan in CI pipelines without a running service, or query cairnmind impact to see which stored decisions, constraints, and docs are affected by changes since HEAD.',
        command: 'cairnmind impact my-app --json',
      },
      {
        name: 'Local answer logging, usage logs & regression replay',
        detail:
          'Inspect local-only session/token logs with cairnmind usage (never sent anywhere), keep masked project answers on disk, and run cairnmind eval replay or cairnmind eval injection to catch citation regressions and prompt-injection exposure.',
        command: 'cairnmind eval injection --runs 3',
      },
    ],
    docHref: '/docs/workflow/',
    docLabel: 'Workflow, checks & CI guide',
  },
  {
    id: 'code-graph-and-storage',
    eyebrow: '04 · Multi-Language Code Graph & Hybrid Retrieval',
    title: 'Tree-sitter + SCIP code graph fused with BM25 and HNSW vectors',
    summary:
      'One local ArcadeDB engine holds documents, dependency edges, full-text indexes, and semantic vectors, plus a lightweight SQLite preview mode for 15-second trials.',
    stability: 'Stable 1.0 + Preview',
    capabilities: [
      {
        name: 'Tree-sitter (Python/JS/TS) + automatic SCIP (9 more languages)',
        detail:
          'Background SCIP worker indexes Go, Rust, Java, Kotlin, C, C++, C#, PHP, and Ruby into the same defines/imports/calls/references/test_imports graph, with offline cairnmind scip import support.',
        command: 'cairnmind scip status --json',
      },
      {
        name: 'Hybrid RAG + GraphRAG retrieval',
        detail:
          'Fuses BM25 full-text search, HNSW vector similarity, definition lookup, and graph traversal, then re-checks every returned passage against live files on disk.',
        command: 'cairnmind index my-app',
      },
      {
        name: 'Pinned ArcadeDB 26.11.1 + lossless SQLite preview',
        detail:
          'Run managed native ArcadeDB (Java 21), digest-pinned Docker container, remote server/Kubernetes connect-only, or zero-dependency SQLite WAL+FTS5 (Preview) with lossless cairnmind migrate --to server.',
        command: 'cairnmind migrate --to server',
      },
      {
        name: 'Checksummed backups & single-writer protection',
        detail:
          'Streamed .cairnmind.json.gz backups (with staged restore & before-restore safety copy) and installation.json single-writer locks prevent accidental split-brain writes.',
        command: 'cairnmind backup backup.cairnmind.json.gz',
      },
    ],
    docHref: '/docs/architecture/',
    docLabel: 'Architecture & storage guide',
  },
  {
    id: 'human-sovereignty-security',
    eyebrow: '05 · Human Sovereignty & Zero-Plugin Security',
    title: 'Only a person confirms, enforced outside the model',
    summary:
      'Trust boundaries live in the OS and storage service, never in polite prompt instructions. Neither an MCP tool nor a CLI call can confirm a record, approve a plan, or bypass the secrets gate.',
    stability: 'Stable 1.0',
    capabilities: [
      {
        name: 'OS-enforced person-confirmation key',
        detail:
          'Confirming/disputing records, resolving conflicts, confirming plans, releasing secrets, or deleting project memory requires the live workspace key (a tmux server on POSIX, an owner-only Named Pipe on native Windows).',
        command: 'cairnmind brief my-app',
      },
      {
        name: 'Secret masking & publication gate',
        detail:
          'Masks registered secrets and high-entropy tokens before indexing or transcript conversion, and blocks publication in .memory/cairnmind-published/ until a person reviews or explicitly releases it.',
        command: 'cairnmind memory publish my-app --preview --json',
      },
      {
        name: 'Sensitivity-class egress control',
        detail:
          'Remote model servers require HTTPS plus --allow-remote consent. Hosted built-in agents obey memory_remote_classes (default ["project"]) and hold back conversations and third-party sources automatically.',
        command: 'cairnmind setup --json',
      },
      {
        name: 'Zero in-process plugins & out-of-process converters',
        detail:
          'Third-party MCP tools and custom history converters (cairnmind importer add / import --via) run strictly out of process with scrubbed environments, bounded timeouts, and degraded provenance.',
        command: 'cairnmind importer list --json',
      },
    ],
    docHref: '/docs/memory-contracts/',
    docLabel: 'Security & memory contracts',
  },
  {
    id: 'workspaces-and-contract',
    eyebrow: '06 · Workspaces, Git Team Memory & 1.0 Contract',
    title: 'Built for terminal engineers, with a frozen 1.0 contract',
    summary:
      'Operate from a multi-pane terminal cockpit, an optional loopback Obsidian web inspector, or pure --json scripts backed by CI-frozen contract fixtures.',
    stability: 'Stable 1.0 + Preview',
    capabilities: [
      {
        name: 'Cross-platform terminal workspaces',
        detail:
          'Multi-pane tmux + Textual cockpit on Linux, WSL2, and macOS. Standalone Textual workspace on native Windows PowerShell, with no tmux requirement and full keyboard navigation.',
        command: 'cairnmind',
      },
      {
        name: 'Local web Engineer Cockpit (Preview)',
        detail:
          'Optional loopback-only (127.0.0.1:4310) Obsidian Glass browser inspector streaming SSE updates for OCC session alignment, task test gates, SHA-256 verbatim diffs, and 4-column blast-radius graphs.',
        command: 'cairnmind ui --port 4310 --project my-app',
      },
      {
        name: 'Portable snapshots & Git team memory',
        detail:
          'Share reviewed decisions, constraints, and plans across teammates through Git (.memory/cairnmind-published/ FORMAT v1) with diff, validate, and zero credentials committed.',
        command: 'cairnmind memory publish my-app',
      },
      {
        name: 'Frozen 1.0 CLI, MCP & storage contract (v0.12 → 1.0)',
        detail: `Contract frozen ahead of 1.0 (no breaking changes planned from v0.12): ${REPO_META.cliContractFixtures} CLI --json shapes, ${REPO_META.mcpContractFixtures} captures across all 8 MCP tools, and cross-release store/backup upgrade fixtures guarded on every PR.`,
        command: 'cairnmind doctor --contract --format text',
      },
    ],
    docHref: '/docs/stable-contract/',
    docLabel: 'Read the 1.0 Stable Contract',
  },
];

export const WHAT_IT_DOES_NOT_DO = [
  {
    claim: 'Does not replace your coding assistant',
    detail:
      'Codex CLI, Claude Code, Copilot, Cursor, and API agents still write code under their own billing and tool permissions. Cairnmind is the local memory, verification, and hand-off service beside them.',
  },
  {
    claim: 'Does not guarantee an LLM understood what it acknowledged',
    detail:
      'Read-before-write ACK proves a session received and echoed the exact plan, task, and state_hash versions. It does not prove the model reasoned flawlessly about them.',
  },
  {
    claim: 'Does not stage, commit, or push Git changes',
    detail:
      'Cairnmind reads Git status, commits, and diffs to compute code fingerprints and impact, and writes published memory directories cleanly on disk, leaving every Git operation to you.',
  },
  {
    claim: 'Does not load third-party plugins inside its own processes',
    detail:
      'Because Cairnmind holds local service and database credentials, all extension surfaces (check profiles, external MCP clients, third-party MCP tool servers, history converters) run strictly out of process.',
  },
  {
    claim: 'Does not verify semantic interpretation beyond source quotes',
    detail:
      'Passing tests prove the declared check profile succeeded on the current code fingerprint. They do not prove that every acceptance criterion was met or reviewed by a human.',
  },
];

export const SUPPORTED_MATRIX = [
  {
    component: 'Operating Systems',
    supported: 'Linux (Ubuntu 24.04 tested), native Windows 10/11 (PowerShell), Windows WSL2 Ubuntu, macOS on Apple silicon',
    notes: 'CI runs fast + full tests on Linux, macOS Apple silicon fast tests, and Windows fast + ArcadeDB tests.',
  },
  {
    component: 'Python & Packaging',
    supported: 'Python 3.12+ via uv',
    notes: 'Isolated uv tool install. Zero website assets in wheel or sdist.',
  },
  {
    component: 'Database Backends',
    supported: 'Pinned ArcadeDB 26.11.1 (Java 21 native, Docker image, or remote/K8s connect-only) · SQLite WAL+FTS5 (Preview)',
    notes: 'Lossless upgrade from SQLite preview via cairnmind migrate --to server.',
  },
  {
    component: 'Local / Remote Models',
    supported: 'Optional: Ollama (qwen3.5:9b + qwen3-embedding:0.6b) or HTTPS OpenAI-compatible server',
    notes: 'Deterministic-only mode works with zero models and zero GPU.',
  },
  {
    component: 'Coding Assistants',
    supported: 'Codex CLI, Claude Code (≥ 2.1.238 for cairnmind-recorder), GitHub Copilot SDK, OpenAI/Anthropic/Ollama API agents',
    notes: 'External MCP presets for Cursor, Cline, Windsurf, VS Code, Roo Code, Gemini CLI, Zed, Claude Desktop, and generic.',
  },
  {
    component: 'Code Graph Languages',
    supported: 'Tree-sitter: Python, JavaScript, TypeScript · Auto SCIP: Go, Rust, Java, Kotlin, C, C++, C#, PHP, Ruby',
    notes: 'All other UTF-8 languages remain indexed and searchable as text.',
  },
];
