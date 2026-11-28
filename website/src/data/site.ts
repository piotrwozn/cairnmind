import { REPO_META } from '@/lib/repoMeta';

export const SITE = {
  name: 'Cairnmind',
  tagline: 'Your coding agent says “Done”. Cairnmind makes it prove it.',
  shortDefinition:
    'Free, open-source, and local. Works beside Claude Code, Codex CLI, Cursor, and Copilot. It blocks “done” until tests pass on current code, makes every answer quote your repo or say “unknown”, and carries plans between agents.',
  repoUrl: 'https://github.com/piotrwozn/cairnmind',
  discussionsUrl: 'https://github.com/piotrwozn/cairnmind/discussions',
  issuesUrl: 'https://github.com/piotrwozn/cairnmind/issues',
  goodFirstIssuesUrl: 'https://github.com/piotrwozn/cairnmind/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22',
  contributingUrl: 'https://github.com/piotrwozn/cairnmind/blob/main/CONTRIBUTING.md',
  releasesUrl: 'https://github.com/piotrwozn/cairnmind/releases',
  licenseUrl: 'https://github.com/piotrwozn/cairnmind/blob/main/LICENSE',
  licenseName: 'Apache-2.0',
  authorName: 'Piotr Woźnicki',
  authorHandle: '@piotrwozn',
  authorUrl: 'https://github.com/piotrwozn',
  currentRelease: REPO_META.version,
  releaseDateIso: '2026-11-19T05:26:54Z',
  defaultSiteUrl: 'https://cairnmind.dev',
  ogImagePath: '/og-image.png',
} as const;

export const NAV_LINKS = [
  { label: 'Features', href: '/features/' },
  { label: 'Install', href: '/install/' },
  { label: 'Benchmarks', href: '/benchmarks/' },
  { label: 'Compare', href: '/compare/' },
  { label: 'Use Cases', href: '/use-cases/' },
  { label: 'Docs', href: '/docs/' },
  { label: 'FAQ', href: '/faq/' },
] as const;

export const FOOTER_COLUMNS = [
  {
    title: 'Product',
    links: [
      { label: 'Overview', href: '/' },
      { label: 'All Capabilities', href: '/features/' },
      { label: 'Install & OS Guides', href: '/install/' },
      { label: 'Measured Benchmarks', href: '/benchmarks/' },
      { label: 'FAQ', href: '/faq/' },
    ],
  },
  {
    title: 'Comparisons & Use Cases',
    links: [
      { label: 'All Comparisons', href: '/compare/' },
      { label: 'vs. CLAUDE.md / AGENTS.md', href: '/compare/vs-claude-md-and-agents-md/' },
      { label: 'vs. Built-in Assistant Memory', href: '/compare/vs-builtin-assistant-memory/' },
      { label: 'vs. Vector & MCP Memory Layers', href: '/compare/vs-mcp-memory-servers/' },
      { label: 'Cross-Agent Handoffs', href: '/use-cases/cross-agent-handoff/' },
      { label: 'Prevent Hallucinated Done', href: '/use-cases/prevent-hallucinated-done/' },
      { label: 'Local Private Code Graph', href: '/use-cases/local-private-code-intelligence/' },
      { label: 'Git Team Memory', href: '/use-cases/git-team-memory/' },
    ],
  },
  {
    title: 'Documentation',
    links: [
      { label: 'Documentation Home', href: '/docs/' },
      { label: 'Installation Guide', href: '/docs/install/' },
      { label: 'Frozen 1.0 Contract', href: '/docs/stable-contract/' },
      { label: 'Workflow & Test Gates', href: '/docs/workflow/' },
      { label: 'Memory & Security Contracts', href: '/docs/memory-contracts/' },
      { label: 'Architecture', href: '/docs/architecture/' },
      { label: 'Changelog', href: '/docs/changelog/' },
    ],
  },
  {
    title: 'Community & Source',
    links: [
      { label: '★ Star on GitHub', href: SITE.repoUrl, external: true },
      { label: 'Discussions', href: SITE.discussionsUrl, external: true },
      { label: 'Good First Issues', href: SITE.goodFirstIssuesUrl, external: true },
      { label: 'Contributing Guide', href: SITE.contributingUrl, external: true },
      { label: 'Releases', href: SITE.releasesUrl, external: true },
      { label: 'llms.txt', href: '/llms.txt' },
      { label: 'Apache-2.0 License', href: SITE.licenseUrl, external: true },
    ],
  },
] as const;

export function getSiteUrl(): string {
  return (process.env.SITE_URL || SITE.defaultSiteUrl).trim().replace(/\/+$/, '');
}

export function isProductionSite(): boolean {
  return getSiteUrl() === SITE.defaultSiteUrl;
}
