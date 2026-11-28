import type { APIRoute } from 'astro';
import { getAllDocs } from '@/lib/docs';
import { COMPARISON_PAGES } from '@/data/comparisons';
import { USE_CASE_PAGES } from '@/data/useCases';
import { FAQ_ITEMS } from '@/data/faq';
import { FEATURE_PILLARS } from '@/data/features';

export const GET: APIRoute = async () => {
  const docs = await getAllDocs();

  const entries = [
    {
      title: 'Cairnmind Overview',
      href: '/',
      section: 'Product',
      excerpt:
        'Verified project memory, verbatim quotes, fingerprint-bound test gates, 12-language SCIP + Tree-sitter code graph, and cold handoffs.',
    },
    {
      title: 'All Implemented Capabilities',
      href: '/features/',
      section: 'Product',
      excerpt: FEATURE_PILLARS.map((p) => `${p.title}: ${p.capabilities.map((c) => c.name).join(', ')}`).join(' · '),
    },
    {
      title: 'Install Cairnmind (Linux, WSL2, macOS, Native Windows, Docker, MCP)',
      href: '/install/',
      section: 'Product',
      excerpt: 'uvx cairnmind try, install-wsl.sh, install-windows.ps1, Docker engine, remote DB, cairnmind mcp generate.',
    },
    {
      title: 'Measured Evaluations & Benchmarks',
      href: '/benchmarks/',
      section: 'Product',
      excerpt: '100% golden recall, 19/20 real-repo Q&A, 20/21 (24/24 on main) absent-feature refusal, 29% extraction rejection, prompt injection eval.',
    },
    ...COMPARISON_PAGES.map((c) => ({
      title: c.title,
      href: `/compare/${c.slug}/`,
      section: 'Compare',
      excerpt: c.intro,
    })),
    ...USE_CASE_PAGES.map((u) => ({
      title: u.title,
      href: `/use-cases/${u.slug}/`,
      section: 'Use Cases',
      excerpt: u.problemStatement,
    })),
    {
      title: 'Technical FAQ',
      href: '/faq/',
      section: 'FAQ',
      excerpt: FAQ_ITEMS.map((f) => f.question).join(' · '),
    },
    ...docs.map((d) => ({
      title: d.title,
      href: `/docs/${d.slug}/`,
      section: `Docs · ${d.group}`,
      excerpt: `${d.description} ${d.plainExcerpt}`.slice(0, 260),
    })),
  ];

  return new Response(JSON.stringify(entries), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
};
