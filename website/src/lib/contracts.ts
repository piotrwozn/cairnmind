import fs from 'node:fs';
import path from 'node:path';
import { findRepoRoot } from '@/lib/repoMeta';

const FIXTURES_ROOT = path.join(findRepoRoot(), 'tests', 'fixtures', 'contracts');

export interface ContractSample {
  id: string;
  label: string;
  category: string;
  command: string;
  fixtureRelPath: string;
  summary: string;
  highlights: {
    field: string;
    meaning: string;
    tone: 'ok' | 'warn' | 'accent';
  }[];
  payloadPretty: string;
}

function readFixture(relPath: string): string {
  const abs = path.join(FIXTURES_ROOT, relPath);
  const raw = fs.readFileSync(abs, 'utf-8');
  return JSON.stringify(JSON.parse(raw), null, 2);
}

export function loadContractSamples(): ContractSample[] {
  return [
    {
      id: 'resume-packet',
      label: 'Resume Packet v2',
      category: 'MCP Tool · Handoff & OCC',
      command: 'mcp.resume_context(full_packet=true)',
      fixtureRelPath: 'tests/fixtures/contracts/mcp_tools/resume_context_full.json',
      summary:
        'What an assistant receives on cold start: active plan version, active task, live worktree code fingerprint, recomputed state_hash, and server-enforced degraded evidence warnings for external clients.',
      highlights: [
        { field: 'packet.schema_version: 2', meaning: 'Versioned resume contract locked for 1.0', tone: 'accent' },
        { field: 'packet.state_hash', meaning: 'Must be acknowledged before any session write succeeds', tone: 'ok' },
        { field: 'packet.evidence: "degraded"', meaning: 'Server-stamped provenance for external MCP clients', tone: 'warn' },
      ],
      payloadPretty: readFixture('mcp_tools/resume_context_full.json'),
    },
    {
      id: 'ask-deterministic',
      label: 'Ask (Zero-Model Mode)',
      category: 'CLI --json · Verified Knowledge',
      command: 'cairnmind ask contract-app "…" --json',
      fixtureRelPath: 'tests/fixtures/contracts/cli_json/ask.json',
      summary:
        'Even without local LLMs configured, Cairnmind refuses to fabricate an answer: it returns status: "unavailable" while surfacing exact matching code and decision passages with line spans.',
      highlights: [
        { field: 'status: "unavailable"', meaning: 'Never fabricates answers when models are off', tone: 'warn' },
        { field: 'full_text_matches[]', meaning: 'Exact code & decision excerpts with file URIs and line numbers', tone: 'ok' },
        { field: 'diagnostics.code', meaning: 'Machine-readable reason ("models_not_configured")', tone: 'accent' },
      ],
      payloadPretty: readFixture('cli_json/ask.json'),
    },
    {
      id: 'pulse-audit',
      label: 'Memory Pulse (<500ms)',
      category: 'CLI --json · CI & Pre-Push',
      command: 'cairnmind pulse contract-app --json',
      fixtureRelPath: 'tests/fixtures/contracts/cli_json/pulse.json',
      summary:
        'Fast read-only consistency audit designed for CI and pre-push hooks: checks backend health, code drift, stale test tasks, broken snapshot references, and whether done would be blocked.',
      highlights: [
        { field: 'status: "healthy"', meaning: 'Exit 0 healthy, 1 drift under --strict, 2 broken', tone: 'ok' },
        { field: 'stale_test_tasks[]', meaning: 'Flags any requires_test task invalidated by code edits', tone: 'warn' },
        { field: 'would_block_done', meaning: 'Direct boolean gate for automated pipelines', tone: 'accent' },
      ],
      payloadPretty: readFixture('cli_json/pulse.json'),
    },
    {
      id: 'publish-gate',
      label: 'Team Publish Gate',
      category: 'CLI --json · Human Sovereignty',
      command: 'cairnmind memory publish contract-app --preview --json',
      fixtureRelPath: 'tests/fixtures/contracts/cli_json/memory_publish_preview.json',
      summary:
        'Dry-run preview of Git-tracked team memory (.memory/cairnmind-published): automatically holds back unconfirmed proposals and unconfirmed plans, and guarantees zero Git mutations.',
      highlights: [
        { field: 'git: "nothing was staged..."', meaning: 'Hard zero-Git contract verified in every payload', tone: 'ok' },
        { field: 'held.items[]', meaning: 'Unconfirmed proposals held back until a person confirms them', tone: 'warn' },
        { field: 'destination', meaning: 'Deterministic FORMAT v1 output path inside the repository', tone: 'accent' },
      ],
      payloadPretty: readFixture('cli_json/memory_publish_preview.json'),
    },
    {
      id: 'scip-graph',
      label: 'Multi-Language SCIP',
      category: 'CLI --json · Code Graph',
      command: 'cairnmind scip status --json',
      fixtureRelPath: 'tests/fixtures/contracts/cli_json/scip_status.json',
      summary:
        'Automatic background SCIP indexer registry extending Tree-sitter (Python/JS/TS) across Rust, Go, Java, Kotlin, C, C++, C#, PHP, and Ruby inside the same ArcadeDB code graph.',
      highlights: [
        { field: 'indexers (7 runtimes)', meaning: 'Covers 9 additional languages with pinned versions', tone: 'accent' },
        { field: 'mode: "auto"', meaning: 'Configurable auto, manual, or off background indexing', tone: 'ok' },
        { field: 'docker_fallback: true', meaning: 'Optional container fallback for native toolchain gaps', tone: 'warn' },
      ],
      payloadPretty: readFixture('cli_json/scip_status.json'),
    },
    {
      id: 'mcp-preset',
      label: 'Universal MCP Preset',
      category: 'CLI --json · MCP Presets',
      command: 'cairnmind mcp generate --client claude-code --json',
      fixtureRelPath: 'tests/fixtures/contracts/cli_json/mcp_generate.json',
      summary:
        'Previews or atomically writes safe deep-merged MCP configurations across supported coding clients with unified diffs, automatic .cairnmind.bak backups, and zero auto-approval flags.',
      highlights: [
        { field: 'diff', meaning: 'Exact unified diff shown before touching editor config files', tone: 'accent' },
        { field: 'entry.args', meaning: 'Isolated Python (-I -m cairnmind mcp) stdio transport', tone: 'ok' },
        { field: 'written: false', meaning: 'Safe dry-run by default until --write is passed', tone: 'warn' },
      ],
      payloadPretty: readFixture('cli_json/mcp_generate.json'),
    },
  ];
}
