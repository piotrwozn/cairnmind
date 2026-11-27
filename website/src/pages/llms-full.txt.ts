import type { APIRoute } from 'astro';
import { SITE } from '@/data/site';
import { REPO_META } from '@/lib/repoMeta';

export const GET: APIRoute = () => {
  const body = `# Cairnmind: Full Technical Reference for LLMs

> ${SITE.shortDefinition}

- Website: ${SITE.defaultSiteUrl}/
- Repository: ${SITE.repoUrl}
- Package Version: ${REPO_META.version}
- Author: Piotr Woznicki (independent Apache-2.0 project dogfooded to build itself under Cairnmind's own test gates and human-key confirmation)
- Scale: ${REPO_META.pythonModulesFormatted} Python modules, ${REPO_META.testFunctionsFormatted} automated tests across ${REPO_META.testSuitesFormatted} test suites (Linux, Windows, macOS CI), ${REPO_META.contractFixturesFormatted} CI-frozen contract fixtures (${REPO_META.cliContractFixtures} CLI \`--json\` + ${REPO_META.mcpContractFixtures} MCP captures across 8 MCP tools)
- License: Apache-2.0 (ArcadeDB licensed separately under Apache-2.0)

## Quick Start Commands

15-second zero-setup sandbox (Python 3.12+ and uv only, ephemeral in-memory SQLite, writes zero files):
\`\`\`bash
uvx cairnmind try
\`\`\`

Lightweight local SQLite preview:
\`\`\`bash
uv tool install cairnmind
cairnmind init --backend sqlite
cairnmind migrate --to server   # lossless upgrade to ArcadeDB at any time
\`\`\`

Full local ArcadeDB stack:
- Linux / WSL2 / macOS: \`bash scripts/install-wsl.sh\`
- Native Windows 10/11 PowerShell: \`pwsh -File scripts/install-windows.ps1\`
- Docker ArcadeDB engine (no local Java): \`cairnmind engine start --docker\`

Register a project and connect assistants:
\`\`\`bash
cairnmind project add my-app ~/src/my-app
cairnmind remember my-app "Accounts lock after five failed logins."
cairnmind brief my-app
cairnmind ask my-app "When do accounts lock?"
cairnmind install my-app
cairnmind run my-app claude
\`\`\`

Generate MCP presets (claude-code, claude-desktop, cursor, windsurf, vscode, cline, roo-code, codex, gemini-cli, zed, generic):
\`\`\`bash
cairnmind mcp generate --client cursor --write
cairnmind mcp test-connection
cairnmind doctor --check-mcp
\`\`\`

## Measured Evaluation Figures (RTX 4080 Super, WSL2, qwen3.5:9b + qwen3-embedding:0.6b)

- \`cairnmind eval run\` (20 golden recall questions, threshold 0.9): 100% (20/20) on 0.11.0 (89 s) and 0.12.0 (101 s). 100% in 6 of 6 runs on 0.8.0.
- \`cairnmind eval memory\`: passed on 0.11.0 (31 s) and 0.12.0 (36 s). Covers extraction, portable transfer across isolated DBs, cited recall, and cross-project abstention.
- \`cairnmind eval extraction --runs 3\`: 29% rejected/duplicate (8 of 28 proposals, down from 65% on 0.10.0), 15 of 15 kept records found in all 3 runs.
- \`cairnmind eval repository .\`: 19 of 20 on 0.12.0 (440 files, 2,383 chunks) and 20 of 20 on 0.11.0, 0 complete and wrong.
- \`cairnmind eval repository . --questions docs/evaluation/absent-questions.json\`: 20 of 21 refused on 0.11.0/0.12.0. 24 of 24 refused on main (22 Nov 2026) after subject and names checks.
- \`cairnmind eval injection --runs 3\` (22 Nov 2026): answers obeyed 0 of 12, built-in agent obeyed 0 of 9 (0 invariants broken), automatic extraction restated canary in 6 of 12 (README & code comment, which is why extracted records stay proposed until human confirmation).

## 1.0 Stable Contract Summary

- Locked in 1.0: documented CLI commands, exit codes (0/1/2/3), \`--json\` shapes (${REPO_META.cliContractFixtures} frozen fixtures), 8 MCP tools (\`ask\`, \`remember\`, \`checkpoint\`, \`save_plan\`, \`next_task\`, \`resume_context\`, \`ack_resume\`, \`check_status\`, ${REPO_META.mcpContractFixtures} frozen captures), resume packet v2, lossless ArcadeDB store upgrades, logical backups (\`.cairnmind.json.gz\`), portable snapshots (v1), published team memory (\`FORMAT.json\` v1), out-of-process transcript converters (\`importers.json\` v1), \`config.json\` / \`CAIRNMIND_*\` env vars, and core safety invariants.
- Preview (not locked in 1.0): embedded SQLite backend (\`--backend sqlite\` / \`migrate\`), NLI/VCR verification cascade (\`cairnmind[nli]\`), and local web Engineer Cockpit (\`cairnmind ui\`).

## Explicit Non-Guarantees

- Does not replace coding assistants or remove their token costs / context limits.
- Does not stage, commit, or push Git changes.
- Does not load third-party plugins inside Cairnmind processes.
- ACK proves receipt/readback of exact versions, not LLM understanding. Passing tests prove check-profile exit code 0 on the current code fingerprint, not every unstated acceptance criterion.
`;

  return new Response(body, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
};
