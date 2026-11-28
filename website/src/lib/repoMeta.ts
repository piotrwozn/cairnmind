import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export function findRepoRoot(): string {
  const starts = [process.cwd(), path.dirname(fileURLToPath(import.meta.url))];
  for (const start of starts) {
    let dir = path.resolve(start);
    for (let i = 0; i < 8; i += 1) {
      if (fs.existsSync(path.join(dir, 'pyproject.toml')) && fs.existsSync(path.join(dir, 'docs'))) {
        return dir;
      }
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return path.resolve(process.cwd(), '..');
}

const REPO_ROOT = findRepoRoot();

function walkFiles(dir: string, predicate: (name: string) => boolean): string[] {
  if (!fs.existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === '__pycache__' || entry.name.startsWith('.')) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...walkFiles(full, predicate));
    } else if (predicate(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

function readPackageVersion(): string {
  try {
    const raw = fs.readFileSync(path.join(REPO_ROOT, 'pyproject.toml'), 'utf-8');
    const match = raw.match(/^\s*version\s*=\s*"([^"]+)"/m);
    if (match?.[1]) return match[1].trim();
  } catch {}
  return '0.12.0';
}

export function sanitizeHeadlineText(text: string): string {
  return text
    .replace(/`/g, '')
    .replace(/\s*—\s*/g, ', ')
    .replace(/\s*;\s*/g, ', ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Extract a concise highlight label for a release version directly from CHANGELOG.md.
 * Looks up `## X.Y.Z` (matching tag without leading `v`) and uses its first `### ...` heading.
 */
export function readChangelogHighlight(versionOrTag: string): string | null {
  const version = versionOrTag.replace(/^v/i, '').trim();
  if (!version) return null;
  try {
    const md = fs.readFileSync(path.join(REPO_ROOT, 'CHANGELOG.md'), 'utf-8');
    const lines = md.split(/\r?\n/);
    let inSection = false;
    for (const line of lines) {
      const h2 = line.match(/^##\s+\[?v?([0-9]+\.[0-9]+\.[0-9]+[^\s\]]*)\]?/i);
      if (h2) {
        if (inSection) break;
        if (h2[1].toLowerCase() === version.toLowerCase()) {
          inSection = true;
          continue;
        }
      }
      if (inSection) {
        const h3 = line.match(/^###\s+(.+)$/);
        if (h3?.[1]) {
          const clean = sanitizeHeadlineText(h3[1]);
          return clean.length > 52 ? `${clean.slice(0, 49).replace(/\s+\S*$/, '')}…` : clean;
        }
      }
    }
  } catch {}
  return null;
}

function countRepoEngineeringScale() {
  const pyModules = walkFiles(path.join(REPO_ROOT, 'src', 'cairnmind'), (n) => n.endsWith('.py')).length;
  const testFiles = walkFiles(path.join(REPO_ROOT, 'tests'), (n) => n.startsWith('test_') && n.endsWith('.py'));
  let testFunctions = 0;
  for (const file of testFiles) {
    try {
      const src = fs.readFileSync(file, 'utf-8');
      const matches = src.match(/^(?:async\s+)?def\s+test_/gm);
      if (matches) testFunctions += matches.length;
    } catch {}
  }
  const cliFixtures = walkFiles(path.join(REPO_ROOT, 'tests', 'fixtures', 'contracts', 'cli_json'), (n) =>
    n.endsWith('.json')
  ).length;
  const mcpFixtures = walkFiles(path.join(REPO_ROOT, 'tests', 'fixtures', 'contracts', 'mcp_tools'), (n) =>
    n.endsWith('.json')
  ).length;
  const totalFixtures = cliFixtures + mcpFixtures;

  return {
    pythonModules: pyModules || 221,
    testSuites: testFiles.length || 383,
    testFunctions: testFunctions || 4419,
    cliContractFixtures: cliFixtures || 35,
    mcpContractFixtures: mcpFixtures || 9,
    contractFixtures: totalFixtures || 44,
  };
}

const version = readPackageVersion();
const scale = countRepoEngineeringScale();
const fmt = new Intl.NumberFormat('en-US');

export const REPO_META = {
  version,
  ...scale,
  pythonModulesFormatted: fmt.format(scale.pythonModules),
  testSuitesFormatted: fmt.format(scale.testSuites),
  testFunctionsFormatted: fmt.format(scale.testFunctions),
  contractFixturesFormatted: fmt.format(scale.contractFixtures),
} as const;
