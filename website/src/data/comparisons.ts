export interface ComparisonRow {
  criterion: string;
  alternative: string;
  cairnmind: string;
}

export interface ComparisonPageData {
  slug: string;
  shortTitle: string;
  seoTitle: string;
  title: string;
  metaDescription: string;
  eyebrow: string;
  intro: string;
  whenAlternativeWins: string[];
  whenCairnmindWins: string[];
  rows: ComparisonRow[];
  technicalTakeaway: string;
  relatedDocs: { label: string; href: string }[];
}

export const COMPARISON_PAGES: ComparisonPageData[] = [
  {
    slug: 'vs-claude-md-and-agents-md',
    shortTitle: 'vs. CLAUDE.md & AGENTS.md',
    seoTitle: 'Cairnmind vs. CLAUDE.md & AGENTS.md Files',
    title: 'Cairnmind vs. CLAUDE.md & AGENTS.md Instruction Files',
    metaDescription:
      'Compare Cairnmind with static CLAUDE.md and AGENTS.md files: verbatim quote checks, cross-assistant handoffs, and fingerprint-bound test gates.',
    eyebrow: 'Comparison · Instruction Files',
    intro:
      'Static instruction files like CLAUDE.md and AGENTS.md are the simplest way to give one coding assistant project conventions. They break down once project decisions change faster than humans edit markdown, when work moves across assistants, or when an agent claims a task is done after editing code.',
    whenAlternativeWins: [
      'Small repositories or solo spikes where a 50-line static style guide is all you need.',
      'Zero runtime setup: plain markdown files require no local service, database, or CLI.',
      'In fact, Cairnmind works alongside them: `cairnmind install` writes a clean marked block into CLAUDE.md / AGENTS.md and excludes its own block from the search index.',
    ],
    whenCairnmindWins: [
      'You want every recalled fact or architectural decision checked verbatim against current files, or explicitly answered `unknown`.',
      'You switch between Codex CLI, Claude Code, Copilot, and Cursor on the same worktree and need checksummed cold handoffs.',
      'You want `done` blocked automatically whenever code changed after the last local passing test run.',
      'You need multi-language code dependency graphs (Tree-sitter + SCIP) and blast-radius `impact` analysis across commits.',
    ],
    rows: [
      {
        criterion: 'Who maintains project knowledge',
        alternative: 'You, by hand in static Markdown files.',
        cairnmind: 'The model proposes and you confirm. Every stored record links to source files, commits, or transcripts.',
      },
      {
        criterion: 'Drift detection against current code',
        alternative: 'None. Stale rules sit in CLAUDE.md silently until a human notices.',
        cairnmind: 'Verbatim quote checks against live files, quote self-healing (`memory heal-audit`), and `cairnmind pulse` drift audits.',
      },
      {
        criterion: 'Cross-assistant compatibility',
        alternative: 'Each tool reads its own convention file. State and task progress are not shared.',
        cairnmind: 'Shared local service and checksummed resume packets across Claude Code, Codex CLI, Cursor, Copilot, and any MCP client.',
      },
      {
        criterion: 'Proof required for "done"',
        alternative: 'None. An assistant can declare completion without running tests.',
        cairnmind: 'Fresh local `cairnmind check run` bound to the task definition and current code fingerprint. A 1-line edit marks checks stale.',
      },
      {
        criterion: 'Human trust boundary',
        alternative: 'Any agent with file write permissions can silently rewrite instruction files.',
        cairnmind: 'Only a person confirms records or plans via the open workspace key (tmux / Windows Named Pipe). Agents can only propose.',
      },
      {
        criterion: 'Setup footprint',
        alternative: 'None (plain files in Git).',
        cairnmind: '15-second `uvx cairnmind try` (SQLite) or full local stack (Python/uv + Java 21 or Docker, with Ollama and tmux optional).',
      },
    ],
    technicalTakeaway:
      'Keep static repo conventions in CLAUDE.md / AGENTS.md. Let Cairnmind handle living architectural decisions, verified Q&A, versioned plans, and fingerprint-bound test evidence.',
    relatedDocs: [
      { label: 'Workflow & Check Profiles', href: '/docs/workflow/' },
      { label: 'Implementation Contracts', href: '/docs/contracts/' },
      { label: 'Installation Guide', href: '/docs/install/' },
    ],
  },
  {
    slug: 'vs-builtin-assistant-memory',
    shortTitle: 'vs. Built-in Assistant Memory',
    seoTitle: 'Cairnmind vs. Built-in Coding Assistant Memory',
    title: 'Cairnmind vs. Built-in Coding Assistant Memory',
    metaDescription:
      'Compare Cairnmind with built-in assistant memory: verbatim source evidence, human-only confirmation, local data control, and cross-tool handoffs.',
    eyebrow: 'Comparison · Built-in Memory',
    intro:
      'Most modern coding assistants include built-in auto-memory or session compaction. That memory is convenient inside a single vendor tool, but it is opaque, unverified against your repository, locked to one vendor, and rarely distinguishes a model guess from a human-confirmed engineering constraint.',
    whenAlternativeWins: [
      'You use only one assistant exclusively and only care about lightweight personal style preferences.',
      'You prefer zero local infrastructure and are comfortable with vendor-hosted opaque summaries.',
    ],
    whenCairnmindWins: [
      'You switch tools mid-feature (e.g., Codex CLI → Claude Code → Codex CLI) and refuse to lose active plans, constraints, or verified test status.',
      'You need auditability: where a finding came from (`origin`), whether a human confirmed it, and the exact source lines it quotes.',
      'You have privacy/egress requirements: local-first storage, secret masking, and `memory_remote_classes` holding sensitive transcripts back from hosted models.',
      'You want versioned team memory traveling through Git (`.memory/cairnmind-published/`) rather than trapped in individual cloud profiles.',
    ],
    rows: [
      {
        criterion: 'Cross-assistant portability',
        alternative: 'Locked to one assistant vendor and session history format.',
        cairnmind: 'Neutral local service serving Codex CLI, Claude Code, Copilot SDK, API agents, and external MCP clients.',
      },
      {
        criterion: 'Verification against source files',
        alternative: 'Usually unchecked prose summaries written by the LLM itself.',
        cairnmind: 'Every `ask` citation must match live source text verbatim. Subject, support, and names checks withhold unsupported claims.',
      },
      {
        criterion: 'Who can mark a rule confirmed',
        alternative: 'The assistant writes and mutates its own memory freely.',
        cairnmind: 'Agents can only write `proposed` records. Confirming or replacing confirmed rules requires an explicit person act in the workspace.',
      },
      {
        criterion: 'Cold hand-off consistency',
        alternative: 'Lossy compaction summaries with no concurrency checks.',
        cairnmind: 'Checksummed resume packet (`schema_version: 2`, `state_hash`), mandatory read-before-write ACK, and OCC `expected_version` locks.',
      },
      {
        criterion: 'Team sharing & Git workflow',
        alternative: 'Per-user or proprietary cloud sync.',
        cairnmind: 'Secret-gated portable snapshots and `.memory/cairnmind-published/` directories committed through Git without credentials.',
      },
      {
        criterion: 'Egress & privacy control',
        alternative: 'Sent to the assistant vendor by default.',
        cairnmind: '100% local ArcadeDB/SQLite + optional local Ollama. Sensitivity-class gating keeps private transcripts away from remote APIs.',
      },
    ],
    technicalTakeaway:
      'Built-in assistant memory optimizes convenience inside one chat window. Cairnmind is an engineering system of record that outlives every model session and vendor.',
    relatedDocs: [
      { label: 'Portable & Team Memory', href: '/docs/memory/' },
      { label: 'External MCP Clients', href: '/docs/external-clients/' },
      { label: 'Evaluations & Benchmarks', href: '/docs/evaluation/' },
    ],
  },
  {
    slug: 'vs-mcp-memory-servers',
    shortTitle: 'vs. Vector & MCP Memory Layers',
    seoTitle: 'Cairnmind vs. Vector & MCP Memory Servers',
    title: 'Cairnmind vs. Generic Vector & MCP Memory Layers (Mem0, Zep, RAG MCPs)',
    metaDescription:
      'Compare Cairnmind with Mem0, Zep, and RAG MCP layers: verbatim verification, 12-language SCIP code graph, test-gated tasks, and OCC plans.',
    eyebrow: 'Comparison · Memory Plugins & RAG MCPs',
    intro:
      'Generic MCP memory servers and vector memory layers (such as Mem0, Zep, or thin semantic-search MCP wrappers) focus on storing and retrieving free-form text embeddings. Coding workflows fail for deeper structural reasons: stale code citations, unverified task completion, concurrent overwrites, missing call graphs, and unreviewed agent self-confirmations.',
    whenAlternativeWins: [
      'You are building a consumer chatbot or general conversational assistant that only needs lightweight user-preference recall.',
      'You specifically want a hosted SaaS memory API rather than a local-first developer workbench.',
    ],
    whenCairnmindWins: [
      'You are engineering real software repositories and need exact verbatim citations, abstention on absent technologies (24/24 refused), and zero fabricated config values.',
      'You want unified code intelligence (Tree-sitter + SCIP graph across 12 languages + BM25 + HNSW vectors) rather than flat text chunks alone.',
      'You need execution discipline: versioned plans with OCC, fingerprint-bound local check runs, `<500 ms` CI `pulse`, and serverless `scan`.',
      'You need hard provenance boundaries where external MCP writes are server-stamped `origin_evidence: degraded` and never silently upgraded to verified truth.',
    ],
    rows: [
      {
        criterion: 'Core unit of storage',
        alternative: 'Unstructured conversation snippets or entity facts in a vector store.',
        cairnmind: 'Versioned decisions, constraints, plans, tasks, check runs, source snapshots, and a multi-language code dependency graph.',
      },
      {
        criterion: 'Hallucination & citation checks',
        alternative: 'Returns nearest-neighbor chunks and relies on the calling LLM not to hallucinate.',
        cairnmind: 'Deterministic verbatim quote verification + subject check + support check + product-names check + optional NLI cascade.',
      },
      {
        criterion: 'Code graph & blast-radius analysis',
        alternative: 'Text chunk similarity only. No symbol definitions or call/import edges.',
        cairnmind: 'Tree-sitter (Python/JS/TS) + automatic SCIP (Go, Rust, Java, Kotlin, C, C++, C#, PHP, Ruby) + `cairnmind impact`.',
      },
      {
        criterion: 'Task execution & test proof',
        alternative: 'Out of scope. The memory layer knows nothing about builds or test runs.',
        cairnmind: 'Named `.cairnmind/checks.toml` profiles bound to live worktree code fingerprints. `done` is blocked when checks are stale.',
      },
      {
        criterion: 'External MCP write trust',
        alternative: 'Any MCP tool call writes at the same trust level as a human.',
        cairnmind: 'Server stamps external MCP writes `origin_evidence: degraded`. Human confirmation requires the local workspace key.',
      },
      {
        criterion: 'Frozen 1.0 contract & eval suite',
        alternative: 'Schemas often change without cross-release fixtures or local refusal evals.',
        cairnmind: 'CI-frozen CLI `--json` + MCP tool schemas, cross-release DB/backup upgrade tests, and 6 explicit real-model eval commands.',
      },
    ],
    technicalTakeaway:
      'Vector search finds similar paragraphs. Cairnmind enforces an auditable engineering workflow: verified quotes, code-graph impact, OCC plans, human confirmation, and fresh machine proof for done.',
    relatedDocs: [
      { label: '1.0 Stable Contract', href: '/docs/stable-contract/' },
      { label: 'Architecture & Code Graph', href: '/docs/architecture/' },
      { label: 'Evaluation Suite', href: '/docs/evaluation/' },
    ],
  },
];

export const HOME_COMPARISON_MATRIX = [
  {
    criterion: 'Who writes & confirms it',
    files: 'You, by hand',
    builtin: 'The model',
    plugin: 'The model',
    cairnmind: 'Model proposes, only a person confirms. Every record is tied to a source.',
  },
  {
    criterion: 'Checked against live source files',
    files: 'No',
    builtin: 'Usually no',
    plugin: 'Usually no',
    cairnmind: 'Every cited quote must match a current file verbatim, or the answer is “unknown”',
  },
  {
    criterion: 'Works across coding assistants',
    files: 'Separate file per tool',
    builtin: 'No (vendor-locked)',
    plugin: 'Depends on plugin',
    cairnmind: 'Claude Code, Codex CLI, Cursor, Copilot, and any MCP client',
  },
  {
    criterion: '"Done" requires fresh test proof',
    files: 'No',
    builtin: 'No',
    plugin: 'Usually no',
    cairnmind: 'Fresh local check run bound to current code fingerprint (1-line edit = stale)',
  },
  {
    criterion: 'Code graph & CI health checks',
    files: 'No',
    builtin: 'No',
    plugin: 'Text vectors only',
    cairnmind: 'Tree-sitter + 9 SCIP languages, blast-radius impact, <500 ms pulse & CI scan',
  },
  {
    criterion: 'Setup footprint',
    files: 'None',
    builtin: 'None',
    plugin: 'Usually 1 command',
    cairnmind: '15 s uvx cairnmind try (SQLite) or Java 21 / Docker + uv (Ollama & tmux optional)',
  },
];
