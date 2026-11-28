import { REPO_META } from '@/lib/repoMeta';

export interface FaqItem {
  id: string;
  category: 'Core & Architecture' | 'Verification & Safety' | 'Setup & Platforms' | 'Project & Licensing';
  question: string;
  answer: string;
  docHref?: string;
  docLabel?: string;
}

export function renderInlineCode(text: string): string {
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return escaped.replace(/`([^`]+)`/g, '<code class="code-inline">$1</code>');
}

export const FAQ_ITEMS: FaqItem[] = [
  {
    id: 'what-is-cairnmind',
    category: 'Core & Architecture',
    question: 'What does Cairnmind do beyond storing coding agent memory?',
    answer:
      'Cairnmind is a complete local verification, planning, code-intelligence, and memory layer for coding agents. Beyond verified Q&A with verbatim quotes, it enforces read-before-write resume packets and optimistic concurrency across assistants, blocks "done" on test-gated tasks until fresh local checks match the current code fingerprint, builds a 12-language Tree-sitter + SCIP code graph for blast-radius impact analysis, provides <500 ms CI pulse and serverless scan checks, and ships both a cross-platform terminal workspace and an optional local Obsidian web inspector.',
    docHref: '/features/',
    docLabel: 'See all 6 capability pillars',
  },
  {
    id: 'do-i-need-gpu-or-ollama',
    category: 'Setup & Platforms',
    question: 'Do I need a GPU, Ollama, or local LLMs to use Cairnmind?',
    answer:
      'No. Models are completely optional. Without Ollama or a configured model server, Cairnmind runs in deterministic-only mode: plans, versioned tasks, fingerprint-bound test checks, code graph indexing, blast-radius impact, brief, pulse, and full-text passage search all work normally, while ask returns status: unavailable with matching passages instead of fabricating answers. You can add local Ollama models (qwen3.5:9b + qwen3-embedding:0.6b, ~7 GB) or an HTTPS OpenAI-compatible server whenever you want cited synthesis and background extraction.',
    docHref: '/docs/install/#5-download-the-models-optional',
    docLabel: 'Deterministic-only mode in INSTALL.md',
  },
  {
    id: 'how-fast-can-i-try',
    category: 'Setup & Platforms',
    question: 'How can I try Cairnmind without installing Java, Docker, or Ollama?',
    answer:
      'Run `uvx cairnmind try`. It executes a 15-second sandbox over the bundled examples/sample-project using an ephemeral in-memory SQLite store. It needs only Python 3.12+ and uv, and writes zero files into your working directory. For persistent local projects without Java or Docker, you can also start with `cairnmind init --backend sqlite` (Preview) and migrate losslessly to ArcadeDB later via `cairnmind migrate --to server`.',
    docHref: '/install/',
    docLabel: 'View all install paths',
  },
  {
    id: 'native-windows-support',
    category: 'Setup & Platforms',
    question: 'Does Cairnmind work on native Windows without WSL2 or tmux?',
    answer:
      'Yes. Cairnmind runs natively on Windows 10/11 via PowerShell (`pwsh -File scripts/install-windows.ps1`), stores data in %LOCALAPPDATA%\\cairnmind, opens the standalone Textual workspace directly in Windows Terminal without tmux, and enforces "only a person confirms" via an owner-only Windows Named Pipe. Both Windows fast tests and full Windows ArcadeDB tests run in CI. WSL2 Ubuntu 24.04, Linux, and macOS Apple silicon are also supported.',
    docHref: '/docs/install/#native-windows-powershell',
    docLabel: 'Native Windows setup guide',
  },
  {
    id: 'which-assistants-supported',
    category: 'Core & Architecture',
    question: 'Which coding assistants and editors work with Cairnmind?',
    answer:
      'Cairnmind has first-class managed launchers for Codex CLI, Claude Code (including the optional cairnmind-recorder subagent), GitHub Copilot SDK, and built-in API agents (OpenAI, Anthropic, Ollama, and OpenAI-compatible servers). For editors and external MCP clients (Cursor, Cline, Windsurf, VS Code, Roo Code, Claude Desktop, Gemini CLI, Zed, and generic stdio clients), `cairnmind mcp generate` writes atomic, backed-up presets with server-enforced `origin_evidence: degraded` attribution.',
    docHref: '/docs/external-clients/',
    docLabel: 'External clients documentation',
  },
  {
    id: 'how-prevents-hallucinations',
    category: 'Verification & Safety',
    question: 'How does Cairnmind prevent agents from hallucinating answers or lying about "done"?',
    answer:
      'Two separate mechanisms enforced in the local service, never by prompts alone. First, every `ask` answer must quote live source text verbatim and pass deterministic subject checking, support verification, and product-names checking (refusing 24 of 24 questions about absent technologies on main) or return `unknown`. Second, tasks marked `requires_test` cannot transition to `done` without fresh local `cairnmind check run` evidence tied to the current worktree code fingerprint. Any one-line code edit immediately marks earlier test passes as stale.',
    docHref: '/benchmarks/',
    docLabel: 'See measured evaluation figures',
  },
  {
    id: 'only-a-person-confirms',
    category: 'Verification & Safety',
    question: 'Can a coding agent confirm its own findings, resolve conflicts, or bypass the secrets gate?',
    answer:
      'No. Confirming or disputing a record, resolving a conflict, confirming a plan, releasing past the secrets gate, accepting unknown transcript record kinds, and deleting project memory all require the in-memory person key held exclusively by the open terminal workspace (the tmux server on POSIX, or an owner-only Named Pipe on native Windows). CLI flags, MCP tools, and service-token HTTP calls are refused by the store.',
    docHref: '/docs/stable-contract/#5-safety-rules',
    docLabel: 'Safety invariants in STABLE_CONTRACT.md',
  },
  {
    id: 'git-operations-and-plugins',
    category: 'Verification & Safety',
    question: 'Does Cairnmind ever commit to Git or load third-party plugins?',
    answer:
      'No on both counts. Cairnmind reads Git state and writes reviewable directories such as `.memory/cairnmind-published/`, but it never stages, commits, or pushes. It also never loads third-party plugin code inside its own processes. Check profiles, external MCP clients, third-party MCP tool servers, and custom transcript converters (`cairnmind importer add`) all run strictly out of process with scrubbed credentials.',
    docHref: '/docs/contracts/#extension-surfaces',
    docLabel: 'Extension surfaces & security model',
  },
  {
    id: 'stable-contract-1x',
    category: 'Core & Architecture',
    question: 'What does the 1.0 stable contract promise not to break?',
    answer: `Across all 1.x releases, Cairnmind locks documented CLI commands, exit codes, and \`--json\` shapes (${REPO_META.cliContractFixtures} frozen fixtures), all 8 MCP tools and the v2 resume packet, lossless store schema upgrades, logical backups (\`.cairnmind.json.gz\`), portable snapshots (v1), published team memory (\`FORMAT.json\` v1), external transcript converters, \`config.json\` / \`CAIRNMIND_*\` env vars, and core safety invariants. Preview surfaces (SQLite backend, NLI/VCR verifier, \`cairnmind ui\`) are clearly separated and explicitly documented.`,
    docHref: '/docs/stable-contract/',
    docLabel: 'Read the full 1.0 Stable Contract',
  },
  {
    id: 'why-the-name-cairnmind',
    category: 'Project & Licensing',
    question: 'Why is it called Cairnmind?',
    answer:
      'A cairn is a stack of stones hikers leave on a ridge to mark a verified trail for the next walker. Cairnmind does the same for coding agents: durable, source-checked decisions, plans, and fresh test proof left cleanly by one session for the next.',
    docHref: '/docs/architecture/',
    docLabel: 'Read how Cairnmind is built',
  },
  {
    id: 'who-builds-cairnmind',
    category: 'Project & Licensing',
    question: 'Who builds Cairnmind and what license is it under?',
    answer: `Cairnmind is an independent open-source project under the Apache License 2.0, created and directed by Piotr Woźnicki. It is dogfooded to build itself: its ${REPO_META.pythonModulesFormatted} Python modules, ${REPO_META.testFunctionsFormatted} automated CI tests, and ${REPO_META.contractFixturesFormatted} frozen CLI/MCP fixtures are developed by coding agents operating under Cairnmind’s own test gates and human-key confirmation. ArcadeDB is licensed separately under Apache 2.0.`,
    docHref: '/docs/releasing/',
    docLabel: 'Inspect the release gate process',
  },
];
