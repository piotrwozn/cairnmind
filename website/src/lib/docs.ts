import fs from 'node:fs';
import path from 'node:path';
import { Marked, type Tokens } from 'marked';
import { createHighlighter, type Highlighter } from 'shiki';
import { SITE } from '@/data/site';
import { findRepoRoot } from '@/lib/repoMeta';

const REPO_ROOT = findRepoRoot();
const DOCS_ROOT = path.join(REPO_ROOT, 'docs');

export interface DocMeta {
  slug: string;
  sourceRelPath: string;
  title: string;
  description: string;
  group: string;
  order: number;
}

export interface DocTocItem {
  id: string;
  text: string;
  depth: 2 | 3;
}

export interface RenderedDoc extends DocMeta {
  html: string;
  toc: DocTocItem[];
  plainExcerpt: string;
  wordCount: number;
  githubUrl: string;
}

export const DOC_GROUPS = [
  'Getting Started',
  'Core Workflows',
  'Memory & Security',
  'Clients & Workspaces',
  'Operations & Deployment',
  'Contracts & Evaluations',
] as const;

const DOC_REGISTRY: DocMeta[] = [
  // Getting Started
  {
    slug: 'install',
    sourceRelPath: 'docs/INSTALL.md',
    title: 'Installing Cairnmind',
    description: 'Step-by-step installation for Linux, WSL2, macOS Apple silicon, native Windows PowerShell, Docker, and optional Ollama models.',
    group: 'Getting Started',
    order: 10,
  },
  {
    slug: 'architecture',
    sourceRelPath: 'docs/ARCHITECTURE.md',
    title: 'How Cairnmind Is Built',
    description: 'System architecture: ArcadeDB storage, Hybrid RAG + GraphRAG, Tree-sitter + SCIP code graph, local models, and repository layout.',
    group: 'Getting Started',
    order: 20,
  },
  {
    slug: 'upgrading',
    sourceRelPath: 'docs/UPGRADING.md',
    title: 'Upgrading Cairnmind',
    description: 'Step-by-step release upgrade guide for Cairnmind: logical backups, schema safety snapshots, native Windows transitions, and legacy DevBrain migrations.',
    group: 'Getting Started',
    order: 30,
  },
  // Core Workflows
  {
    slug: 'workflow',
    sourceRelPath: 'docs/WORKFLOW.md',
    title: 'Workflow, Plans, Checks & CI Scan',
    description: 'Versioned plans, named check profiles, code fingerprints, evidence states, brief, impact, symbol-anchored evidence, and serverless CI scan.',
    group: 'Core Workflows',
    order: 40,
  },
  {
    slug: 'terminal',
    sourceRelPath: 'docs/TERMINAL.md',
    title: 'Terminal Workspace & Inspector',
    description: 'Using the multi-pane tmux + Textual cockpit on POSIX, the standalone Windows workspace, memory inspector, and keyboard shortcuts.',
    group: 'Core Workflows',
    order: 50,
  },
  {
    slug: 'api',
    sourceRelPath: 'docs/API.md',
    title: 'API, Models & Connections',
    description: 'Local knowledge service HTTP API, Ollama and OpenAI-compatible model profiles, Copilot SDK connections, sensitivity classes, and third-party MCP tools.',
    group: 'Core Workflows',
    order: 60,
  },
  // Memory & Security
  {
    slug: 'memory',
    sourceRelPath: 'docs/MEMORY.md',
    title: 'Portable & Team Memory',
    description: 'Portable project snapshots (.memory/cairnmind/), Git-tracked published team memory, secrets gating, and import workflows.',
    group: 'Memory & Security',
    order: 70,
  },
  {
    slug: 'memory-contracts',
    sourceRelPath: 'docs/MEMORY_CONTRACTS.md',
    title: 'Memory & Security Contracts',
    description: 'Formal rules for human confirmation, portable memory format v1, published memory format v1, sensitivity classes, and the secrets gate.',
    group: 'Memory & Security',
    order: 80,
  },
  // Clients & Workspaces
  {
    slug: 'external-clients',
    sourceRelPath: 'docs/EXTERNAL_CLIENTS.md',
    title: 'External MCP Clients',
    description: 'Connecting Cursor, Cline, Windsurf, VS Code, Zed, Gemini CLI, and cairnmind-recorder over stdio MCP with degraded provenance rules.',
    group: 'Clients & Workspaces',
    order: 90,
  },
  // Operations & Deployment
  {
    slug: 'storage-and-maintenance',
    sourceRelPath: 'docs/STORAGE_AND_MAINTENANCE.md',
    title: 'Storage, Backups & Maintenance',
    description: 'Data directory layout, checksummed logical backups, staged restore, single-writer store locks, pruning, and maintenance.',
    group: 'Operations & Deployment',
    order: 100,
  },
  {
    slug: 'deployment',
    sourceRelPath: 'docs/DEPLOYMENT.md',
    title: 'Deployment, Docker & Kubernetes',
    description: 'Running ArcadeDB in containers, connect-only installations, two-machine remote database trials, and Kubernetes manifests.',
    group: 'Operations & Deployment',
    order: 110,
  },
  {
    slug: 'releasing',
    sourceRelPath: 'docs/RELEASING.md',
    title: 'Release Gate & Qualification',
    description: 'Cairnmind release gate: three green main runs, adversarial core attack rounds, 10-tester major gate, and 7-day RC freeze.',
    group: 'Operations & Deployment',
    order: 120,
  },
  // Contracts & Evaluations
  {
    slug: 'stable-contract',
    sourceRelPath: 'docs/STABLE_CONTRACT.md',
    title: '1.0.0 Stable Contract',
    description: 'What Cairnmind guarantees not to break across 1.x releases: CLI --json, MCP tools, persistent formats, safety invariants, and preview boundaries.',
    group: 'Contracts & Evaluations',
    order: 130,
  },
  {
    slug: 'contracts',
    sourceRelPath: 'docs/CONTRACTS.md',
    title: 'Implementation Contracts & Limits',
    description: 'Architecture taxonomy, resume packet schema v2, guarantees vs. non-guarantees, known limits, and out-of-process extension surfaces.',
    group: 'Contracts & Evaluations',
    order: 140,
  },
  {
    slug: 'arcade-contracts',
    sourceRelPath: 'docs/ARCADE_CONTRACTS.md',
    title: 'ArcadeDB Storage Contracts',
    description: 'Formal ArcadeDB 26.11.1 storage contract: schema ownership, named-parameter HTTP DAL transactions, graph identity, and single-writer enforcement.',
    group: 'Contracts & Evaluations',
    order: 150,
  },
  {
    slug: 'v03-contracts',
    sourceRelPath: 'docs/V03_CONTRACTS.md',
    title: 'Workflow & Launcher Contracts',
    description: 'Session launcher protocol, read-before-write ACK rules, task verification gates, external degraded MCP sessions, and structured tool schemas.',
    group: 'Contracts & Evaluations',
    order: 160,
  },
  {
    slug: 'evaluation',
    sourceRelPath: 'docs/EVALUATION.md',
    title: 'Explicit Evaluations',
    description: 'Six real-model evaluation harnesses: golden recall, portable memory, conversation extraction, repository Q&A, prompt injection, and answer replay.',
    group: 'Contracts & Evaluations',
    order: 170,
  },
  {
    slug: 'evaluation/results',
    sourceRelPath: 'docs/evaluation/RESULTS.md',
    title: 'Measured Evaluation Results',
    description: 'Full repository evaluation logs, Ollama native vs. /v1 path comparisons, absent-question refusal series, and prompt-injection measurements.',
    group: 'Contracts & Evaluations',
    order: 180,
  },
  {
    slug: 'changelog',
    sourceRelPath: 'CHANGELOG.md',
    title: 'Changelog',
    description: 'Complete Cairnmind release history: breaking changes, contract additions, evaluation milestones, and platform fixes across every published version.',
    group: 'Contracts & Evaluations',
    order: 190,
  },
];

// Map normalized repo-relative paths -> /docs/<slug>/
const REL_PATH_TO_SLUG = new Map<string, string>(
  DOC_REGISTRY.map((d) => [d.sourceRelPath.toLowerCase(), d.slug])
);

/**
 * Exact GitHub heading anchor algorithm matching tests/test_doc_links.py.
 */
export function githubAnchor(heading: string): string {
  const withoutLinks = heading.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  const withoutTags = withoutLinks.replace(/<[^>]+>/g, '');
  return withoutTags
    .trim()
    .toLowerCase()
    .replace(/[^\w\- ]/g, '')
    .replace(/ /g, '-');
}

function resolveDocHref(rawHref: string, currentSourceRelPath: string): { href: string; external: boolean } {
  if (!rawHref || rawHref.startsWith('#')) {
    return { href: rawHref, external: false };
  }
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(rawHref)) {
    return { href: rawHref, external: true };
  }

  const hashIndex = rawHref.indexOf('#');
  const filePart = hashIndex >= 0 ? rawHref.slice(0, hashIndex) : rawHref;
  const fragment = hashIndex >= 0 ? rawHref.slice(hashIndex) : '';

  const currentAbsDir = path.dirname(path.join(REPO_ROOT, currentSourceRelPath));
  const resolvedAbs = path.resolve(currentAbsDir, filePart);
  const relToRepo = path.relative(REPO_ROOT, resolvedAbs).replace(/\\/g, '/');

  // Direct doc match in our static /docs/* portal
  const targetSlug = REL_PATH_TO_SLUG.get(relToRepo.toLowerCase());
  if (targetSlug) {
    return { href: `/docs/${targetSlug}/${fragment}`, external: false };
  }

  // Known synced media assets
  if (relToRepo === 'docs/assets/demo-60s.svg') {
    return { href: '/media/demo-60s.svg', external: false };
  }
  if (relToRepo === 'docs/assets/handoff-demo.gif') {
    return { href: '/media/handoff-demo.gif', external: false };
  }

  // Everything else inside the repo points cleanly to GitHub main
  const isDir = fs.existsSync(resolvedAbs) && fs.statSync(resolvedAbs).isDirectory();
  const kind = isDir ? 'tree' : 'blob';
  const cleanRel = relToRepo.replace(/^\/+/, '');
  return {
    href: `${SITE.repoUrl}/${kind}/main/${cleanRel}${fragment}`,
    external: true,
  };
}

let highlighterPromise: Promise<Highlighter> | null = null;
function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      themes: ['github-dark-default', 'github-light-default'],
      langs: ['bash', 'sh', 'powershell', 'json', 'toml', 'yaml', 'python', 'typescript', 'text', 'diff', 'sql', 'markdown'],
    });
  }
  return highlighterPromise;
}

const LANG_ALIASES: Record<string, string> = {
  shell: 'bash',
  zsh: 'bash',
  console: 'bash',
  pwsh: 'powershell',
  ps1: 'powershell',
  py: 'python',
  ts: 'typescript',
  js: 'typescript',
  yml: 'yaml',
  md: 'markdown',
  txt: 'text',
  plaintext: 'text',
  '': 'text',
};

function stripMarkdownTitle(raw: string): { body: string; extractedH1: string | null } {
  const lines = raw.replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length && lines[i].trim() === '') i += 1;
  const match = lines[i]?.match(/^#\s+(.+?)\s*#*\s*$/);
  if (match) {
    lines.splice(i, 1);
    return { body: lines.join('\n').trimStart(), extractedH1: match[1].trim() };
  }
  return { body: raw, extractedH1: null };
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function makePlainExcerpt(markdownBody: string): string {
  return markdownBody
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#+\s+/gm, '')
    .replace(/[*_>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 320);
}

let cachedDocsPromise: Promise<RenderedDoc[]> | null = null;

export async function getAllDocs(): Promise<RenderedDoc[]> {
  if (cachedDocsPromise) return cachedDocsPromise;

  cachedDocsPromise = (async () => {
    const highlighter = await getHighlighter();
    const loadedLangs = new Set(highlighter.getLoadedLanguages());
    const results: RenderedDoc[] = [];

    for (const meta of DOC_REGISTRY) {
      const absPath = path.join(REPO_ROOT, meta.sourceRelPath);
      if (!fs.existsSync(absPath)) continue;

      const raw = fs.readFileSync(absPath, 'utf-8');
      const { body, extractedH1 } = stripMarkdownTitle(raw);

      const seenAnchors = new Map<string, number>();
      const toc: DocTocItem[] = [];

      const marked = new Marked({ gfm: true, breaks: false });
      const renderer = new marked.Renderer();

      renderer.heading = function (this: unknown, token: Tokens.Heading) {
        const innerHtml = (this as { parser: { parseInline: (t: Tokens.Generic[]) => string } }).parser.parseInline(
          token.tokens
        );
        const base = githubAnchor(token.text);
        const count = seenAnchors.get(base) ?? 0;
        seenAnchors.set(base, count + 1);
        const id = count === 0 ? base : `${base}-${count}`;

        // Ensure no second <h1> appears in rendered doc body
        const safeDepth = token.depth <= 1 ? 2 : token.depth;
        if (safeDepth === 2 || safeDepth === 3) {
          const plainLabel = token.text.replace(/`([^`]+)`/g, '$1').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
          toc.push({ id, text: plainLabel, depth: safeDepth });
        }
        return `<h${safeDepth} id="${escapeHtml(id)}"><a class="doc-anchor" href="#${escapeHtml(id)}" aria-label="Link to ${escapeHtml(token.text)}">${innerHtml}</a></h${safeDepth}>\n`;
      };

      renderer.link = function (this: unknown, token: Tokens.Link) {
        const text = (this as { parser: { parseInline: (t: Tokens.Generic[]) => string } }).parser.parseInline(
          token.tokens
        );
        const resolved = resolveDocHref(token.href, meta.sourceRelPath);
        const titleAttr = token.title ? ` title="${escapeHtml(token.title)}"` : '';
        const extAttrs = resolved.external ? ' target="_blank" rel="noopener noreferrer"' : '';
        return `<a href="${escapeHtml(resolved.href)}"${titleAttr}${extAttrs}>${text}</a>`;
      };

      renderer.image = function (token: Tokens.Image) {
        const resolved = resolveDocHref(token.href, meta.sourceRelPath);
        const alt = escapeHtml(token.text || '');
        return `<img src="${escapeHtml(resolved.href)}" alt="${alt}" loading="lazy" decoding="async" />`;
      };

      renderer.code = function (token: Tokens.Code) {
        const rawLang = (token.lang || '').trim().toLowerCase().split(/\s+/)[0] || '';
        const mapped = LANG_ALIASES[rawLang] ?? rawLang;
        const lang = loadedLangs.has(mapped) ? mapped : 'text';
        try {
          return highlighter.codeToHtml(token.text, {
            lang,
            themes: {
              dark: 'github-dark-default',
              light: 'github-light-default',
            },
            defaultColor: false,
          });
        } catch {
          return `<pre class="code-block"><code>${escapeHtml(token.text)}</code></pre>`;
        }
      };

      marked.use({ renderer });
      const html = await marked.parse(body);
      const plainExcerpt = makePlainExcerpt(body);
      const wordCount = body.split(/\s+/).filter(Boolean).length;

      results.push({
        ...meta,
        title: meta.title || extractedH1 || meta.slug,
        html,
        toc,
        plainExcerpt,
        wordCount,
        githubUrl: `${SITE.repoUrl}/blob/main/${meta.sourceRelPath}`,
      });
    }

    return results.sort((a, b) => a.order - b.order);
  })();

  return cachedDocsPromise;
}

export function getDocsRoot(): string {
  return DOCS_ROOT;
}
