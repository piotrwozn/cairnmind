import { SITE, getSiteUrl } from '@/data/site';
import type { FaqItem } from '@/data/faq';

export interface BreadcrumbItem {
  label: string;
  href: string;
}

export function buildCanonical(pathname: string): string {
  const base = getSiteUrl();
  const normalized = pathname.startsWith('/') ? pathname : `/${pathname}`;
  const withSlash = normalized.endsWith('/') || normalized.includes('.') ? normalized : `${normalized}/`;
  return `${base}${withSlash}`;
}

export function webSiteJsonLd(): Record<string, unknown> {
  const siteUrl = getSiteUrl();
  return {
    '@context': 'https://schema.org',
    '@type': 'WebSite',
    name: SITE.name,
    url: `${siteUrl}/`,
    description: SITE.shortDefinition,
    inLanguage: 'en',
  };
}

export function organizationJsonLd(): Record<string, unknown> {
  const siteUrl = getSiteUrl();
  return {
    '@context': 'https://schema.org',
    '@type': 'Organization',
    name: SITE.name,
    url: `${siteUrl}/`,
    logo: `${siteUrl}/favicon.svg`,
    sameAs: [SITE.repoUrl],
  };
}

export function softwareApplicationJsonLd(): Record<string, unknown> {
  const siteUrl = getSiteUrl();
  return {
    '@context': 'https://schema.org',
    '@type': 'SoftwareApplication',
    name: SITE.name,
    applicationCategory: 'DeveloperApplication',
    operatingSystem: 'Linux, Windows 10, Windows 11, WSL2, macOS (Apple silicon)',
    softwareVersion: SITE.currentRelease,
    description: SITE.shortDefinition,
    url: `${siteUrl}/`,
    codeRepository: SITE.repoUrl,
    downloadUrl: SITE.releasesUrl,
    license: 'https://www.apache.org/licenses/LICENSE-2.0',
    isAccessibleForFree: true,
    offers: {
      '@type': 'Offer',
      price: '0',
      priceCurrency: 'USD',
    },
    author: {
      '@type': 'Person',
      name: SITE.authorName,
      url: 'https://github.com/piotrwozn',
    },
    keywords:
      'coding agents, verified project memory, MCP server, Claude Code, Codex CLI, code graph, SCIP, Tree-sitter, ArcadeDB, local-first RAG, test-gated AI agents',
  };
}

export function faqPageJsonLd(items: FaqItem[]): Record<string, unknown> {
  return {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    mainEntity: items.map((item) => ({
      '@type': 'Question',
      name: item.question,
      acceptedAnswer: {
        '@type': 'Answer',
        text: item.answer,
      },
    })),
  };
}

export function breadcrumbListJsonLd(items: BreadcrumbItem[]): Record<string, unknown> {
  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: items.map((item, idx) => ({
      '@type': 'ListItem',
      position: idx + 1,
      name: item.label,
      item: buildCanonical(item.href),
    })),
  };
}

export function techArticleJsonLd(opts: {
  headline: string;
  description: string;
  pathname: string;
}): Record<string, unknown> {
  const siteUrl = getSiteUrl();
  return {
    '@context': 'https://schema.org',
    '@type': 'TechArticle',
    headline: opts.headline,
    description: opts.description,
    url: buildCanonical(opts.pathname),
    inLanguage: 'en',
    image: `${siteUrl}${SITE.ogImagePath}`,
    author: {
      '@type': 'Person',
      name: SITE.authorName,
      url: 'https://github.com/piotrwozn',
    },
    publisher: {
      '@type': 'Organization',
      name: SITE.name,
      url: `${siteUrl}/`,
    },
  };
}
