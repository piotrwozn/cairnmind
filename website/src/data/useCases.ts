export interface UseCasePageData {
  slug: string;
  eyebrow: string;
  seoTitle: string;
  title: string;
  shortTitle: string;
  metaDescription: string;
  problemStatement: string;
  howCairnmindSolvesIt: string[];
  workflowSteps: {
    step: string;
    command: string;
    outcome: string;
  }[];
  guarantees: string[];
  limits: string[];
  relatedDocs: { label: string; href: string }[];
}

export const USE_CASE_PAGES: UseCasePageData[] = [
  {
    slug: 'cross-agent-handoff',
    eyebrow: 'Use Case 01 · Multi-Assistant Continuity',
    seoTitle: 'Cold Handoffs: Claude Code, Codex & Cursor | Cairnmind',
    title: 'Cold Handoffs Between Claude Code, Codex CLI, Copilot & Cursor',
    shortTitle: 'Cross-Agent Handoffs',
    metaDescription:
      'Hand off versioned plans, verified decisions, and tasks across Claude Code, Codex CLI, Copilot, and Cursor using checksummed resume packets.',
    problemStatement:
      'When you hit rate limits in Claude Code, switch to Codex CLI for a refactor, or open Cursor for UI work, every new session starts cold. Pasting chat transcripts wastes context tokens and silently drops architectural constraints or in-flight task versions.',
    howCairnmindSolvesIt: [
      'Cairnmind maintains one local project store outside every assistant process, holding versioned plans, task dependencies, source snapshots, and reviewed architectural decisions.',
      'When a fresh assistant connects via MCP, it receives a bounded, checksummed resume packet (schema_version: 2) with a recomputed state_hash covering the active plan, next task, scoped records, and current worktree code fingerprint.',
      'Session writes are refused until the assistant calls ack_resume with the exact packet and plan/task versions. If another session updates the plan concurrently, expected_version CAS rejects stale overwrites.',
      'External MCP clients (Cursor, Cline, Windsurf, Zed, VS Code) participate safely through cairnmind mcp generate presets, with their writes transparently stamped origin_evidence: degraded.',
    ],
    workflowSteps: [
      {
        step: '1. Bind installed assistants to the project',
        command: 'cairnmind install my-app',
        outcome: 'Configures MCP entries, hooks, instruction blocks, and optional cairnmind-recorder for installed clients only.',
      },
      {
        step: '2. Start session A (e.g. Claude Code)',
        command: 'cairnmind run my-app claude',
        outcome: 'Session acknowledges resume packet v2, saves a proposed decision and versioned plan, and records checkpoint state.',
      },
      {
        step: '3. Hand off cold to session B (e.g. Codex CLI)',
        command: 'cairnmind run my-app codex',
        outcome: 'Fresh Codex session loads the exact plan version, active task, source quotes, and code fingerprint without pasting chat history.',
      },
      {
        step: '4. Inspect alignment or connect an external IDE',
        command: 'cairnmind mcp generate --client cursor --write',
        outcome: 'Atomically merges an external MCP preset with backup and zero auto-approval flags.',
      },
    ],
    guarantees: [
      'Session writes never succeed without acknowledging the live state_hash and exact plan/task versions.',
      'Concurrent sessions cannot silently overwrite plan steps thanks to optimistic concurrency (expected_version).',
      'External MCP writes are always server-marked degraded and never shown as verified evidence.',
    ],
    limits: [
      'ACK proves receipt and readback of specific versions, not that the model reasoned perfectly about them.',
      'Each coding assistant still uses its own account billing, context window, and native tool permissions.',
    ],
    relatedDocs: [
      { label: 'Workflow & Resume Protocol', href: '/docs/workflow/' },
      { label: 'External MCP Clients', href: '/docs/external-clients/' },
      { label: 'Terminal Workspace', href: '/docs/terminal/' },
    ],
  },
  {
    slug: 'prevent-hallucinated-done',
    eyebrow: 'Use Case 02 · Execution Verification & CI Gates',
    seoTitle: 'Stop Coding Agents Faking Done | Cairnmind',
    title: 'Stop Coding Agents from Hallucinating Facts and Faking "Done"',
    shortTitle: 'Prevent Hallucinated Done',
    metaDescription:
      'Block stale test claims and fabricated config answers with code-fingerprint check profiles, verbatim quote verification, pulse, and CI scan.',
    problemStatement:
      'Coding agents frequently cite stale test output after making "one quick cleanup edit," or answer questions about ports, flags, and frameworks that do not exist in the repository with confident, plausible-sounding numbers.',
    howCairnmindSolvesIt: [
      'Named check profiles declared in .cairnmind/checks.toml run locally without a shell and bind their passing exit code to the exact task definition and worktree code fingerprint.',
      'Any single-line edit in the worktree immediately marks earlier check runs stale. Marking a requires_test task done is refused until a fresh passing run matches the current code.',
      'For factual questions, ask requires verbatim quotes verified against live files plus deterministic subject checks, support checks, and product-names checks. On main, it refused 24 of 24 absent-feature questions.',
      'In CI and pre-push hooks, cairnmind pulse --strict (< 500 ms) catches drift and stale test gates, while cairnmind scan runs serverless checks without starting the local service.',
    ],
    workflowSteps: [
      {
        step: '1. Declare deterministic check profiles in Git',
        command: 'cairnmind check profiles my-app --json',
        outcome: 'Validates argv, timeout, and output budgets from .cairnmind/checks.toml.',
      },
      {
        step: '2. Run check bound to a task & current fingerprint',
        command: 'cairnmind check run my-app unit --task TASK_ID',
        outcome: 'Stores local_runner evidence linked to the exact Git head and dirty file hashes. Editing code flips status to stale.',
      },
      {
        step: '3. Gate CI or pre-push in under 500 ms',
        command: 'cairnmind pulse my-app --strict --json',
        outcome: 'Exits 0 when healthy, 1 on drift/stale tasks under --strict, and 2 on broken snapshot/integrity state.',
      },
      {
        step: '4. Audit answers with regression replay',
        command: 'cairnmind eval replay my-app',
        outcome: 'Re-asks logged project questions after upgrades and flags lost citations or status regressions.',
      },
    ],
    guarantees: [
      'A requires_test task cannot move to done on reported chat text alone. Fresh local_runner proof is enforced in the store.',
      'Checks are never executed by MCP tools or the memory service.',
      'Verbatim quotes shown in ask are always taken literally from the current source file on disk.',
    ],
    limits: [
      'Passing tests verify that your declared check profile exited 0 on the current code. They cannot verify unstated acceptance criteria.',
      'Without local models configured, ask operates in deterministic-only full-text search mode and reports status: unavailable rather than synthesizing answers.',
    ],
    relatedDocs: [
      { label: 'Workflow & Named Check Profiles', href: '/docs/workflow/' },
      { label: 'Implementation Contracts', href: '/docs/contracts/' },
      { label: 'Evaluation & Replay', href: '/docs/evaluation/' },
    ],
  },
  {
    slug: 'local-private-code-intelligence',
    eyebrow: 'Use Case 03 · Local-First Code Graph & Privacy',
    seoTitle: 'Local SCIP & Tree-sitter Code Graph | Cairnmind',
    title: 'Local Multi-Language Code Graph (Tree-sitter + SCIP) & Egress Control',
    shortTitle: 'Local Private Code Graph',
    metaDescription:
      'Index 12 languages locally with Tree-sitter, SCIP, and ArcadeDB Hybrid RAG while enforcing secret masking and sensitivity-class egress rules.',
    problemStatement:
      'Cloud code-indexing tools upload entire proprietary repositories and chat transcripts to third-party servers, while basic local grep lacks symbol dependency graphs, blast-radius analysis, and semantic recall.',
    howCairnmindSolvesIt: [
      'Tree-sitter analyzes Python, JavaScript, and TypeScript directly, while Cairnmind’s asynchronous SCIP worker indexes Go, Rust, Java, Kotlin, C, C++, C#, PHP, and Ruby into the same local ArcadeDB code graph (defines, imports, calls, references, test_imports).',
      'Hybrid RAG + GraphRAG fuses BM25 full-text search, HNSW vector embeddings, definition lookups, and graph traversal locally on your workstation, or runs in zero-LLM deterministic-only mode.',
      'Registered secrets and high-entropy tokens are masked before indexing or transcript import. Cairnmind also respects .cairnmindignore and .gitignore automatically.',
      'When you optionally connect hosted models or built-in API agents, memory_remote_classes (default ["project"]) automatically holds back private conversation and third-party sources.',
    ],
    workflowSteps: [
      {
        step: '1. Inspect topology and data-egress boundaries',
        command: 'cairnmind setup --json',
        outcome: 'Reports where storage runs, where each model runs, and whether any memory text leaves your machine.',
      },
      {
        step: '2. Check multi-language SCIP indexers',
        command: 'cairnmind scip status --json',
        outcome: 'Lists detected repository languages and background SCIP indexer readiness (auto, manual, or off).',
      },
      {
        step: '3. Measure blast radius before merging changes',
        command: 'cairnmind impact my-app --json',
        outcome: 'Finds stored decisions, constraints, and docs potentially affected by modified symbols and files since HEAD.',
      },
      {
        step: '4. Optional remote DB or Kubernetes deployment',
        command: 'cairnmind setup remote-db --url https://… --password-file ~/.secrets/db',
        outcome: 'Connects securely over TLS with private 0600 credential files (never passing passwords on CLI arguments).',
      },
    ],
    guarantees: [
      'Zero telemetry, zero cloud lock-in, and explicit --allow-remote + HTTPS consent required before any model call leaves loopback.',
      'Single-writer database locking prevents concurrent installations from corrupting a shared store.',
      'No third-party plugin code is ever loaded inside a Cairnmind process.',
    ],
    limits: [
      'Static analysis covers 12 languages via Tree-sitter + SCIP. Dynamic runtime calls cannot be resolved statically, and other languages stay searchable as text.',
      'Hosted coding clients (Codex CLI, Claude Code, external MCP) still send whatever they read to their own model provider.',
    ],
    relatedDocs: [
      { label: 'Architecture & Code Graph', href: '/docs/architecture/' },
      { label: 'API, Models & Sensitivity Classes', href: '/docs/api/' },
      { label: 'Deployment & Containers', href: '/docs/deployment/' },
    ],
  },
  {
    slug: 'git-team-memory',
    eyebrow: 'Use Case 04 · Team Engineering Memory',
    seoTitle: 'Secret-Gated Git Team Memory | Cairnmind',
    title: 'Versioned, Secret-Gated Team Memory Through Git',
    shortTitle: 'Git Team Memory',
    metaDescription:
      'Share person-confirmed decisions, constraints, and plans across teammates through Git without credentials using Cairnmind published memory.',
    problemStatement:
      'When one engineer discovers an undocumented production invariant with an AI assistant, that insight dies in their local session or gets pasted into a wiki that drifts out of date within a sprint.',
    howCairnmindSolvesIt: [
      'Cairnmind exports person-confirmed decisions, constraints, and plans into a clean, reviewable directory inside the repository (.memory/cairnmind-published/, FORMAT v1) that moves through normal Git pull requests.',
      'Unconfirmed model proposals are automatically held back from publication until a human reviews and confirms them in the workspace inspector.',
      'A built-in secrets gate scans every candidate record against .cairnmind/publication.toml and high-entropy/well-known secret patterns, blocking leaks before a file is ever written.',
      'Teammates validate, diff, and import published memory cleanly into their own local store. Imported confirmations stay clearly marked as confirmed elsewhere until adopted locally.',
    ],
    workflowSteps: [
      {
        step: '1. Preview exactly what would be published',
        command: 'cairnmind memory publish my-app --preview --json',
        outcome: 'Lists eligible confirmed records and explicitly names every held-back proposal or secret hit without writing files.',
      },
      {
        step: '2. Write reviewable team memory into the repository',
        command: 'cairnmind memory publish my-app',
        outcome: 'Writes deterministic FORMAT v1 files to .memory/cairnmind-published/ without staging, committing, or pushing.',
      },
      {
        step: '3. Verify integrity in pull requests',
        command: 'cairnmind pulse my-app --strict',
        outcome: 'Checks snapshot and published memory integrity alongside code drift in under 500 ms.',
      },
      {
        step: '4. Import on a teammate’s workstation',
        command: 'cairnmind memory import my-app .memory/cairnmind-published',
        outcome: 'Imports reviewed team knowledge while preserving origin attribution and human-confirmation rules.',
      },
    ],
    guarantees: [
      'Unconfirmed agent proposals are held back automatically from team publication.',
      'Secrets are masked and gated before leaving a machine. Overriding a block requires an explicit person act.',
      'FORMAT v1 published memory and portable snapshots are guaranteed backward-compatible across 1.x releases.',
    ],
    limits: [
      'Portable snapshots (.memory/cairnmind/) can include conversation excerpts when requested. Always inspect them before sharing outside your team.',
      'Git staging, code review, and commits remain explicit human steps.',
    ],
    relatedDocs: [
      { label: 'Portable & Team Memory Guide', href: '/docs/memory/' },
      { label: 'Memory Contracts & Secrets Gate', href: '/docs/memory-contracts/' },
      { label: '1.0 Stable Contract', href: '/docs/stable-contract/' },
    ],
  },
];
