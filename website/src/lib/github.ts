import { SITE } from '@/data/site';
import { REPO_META, readChangelogHighlight, sanitizeHeadlineText } from '@/lib/repoMeta';

/**
 * Build-time GitHub repository stats & latest release metadata.
 * Zero client-side requests, zero third-party scripts, graceful offline fallback
 * backed automatically by pyproject.toml and CHANGELOG.md.
 */
export interface RepoStats {
  stars: number | null;
  starsFormatted: string | null;
  releaseTag: string;
  releaseVersion: string;
  releaseHighlight: string;
  releasePublishedAt: string;
  releaseDateFormatted: string;
}

let cached: Promise<RepoStats> | null = null;

function formatStars(count: number): string {
  if (count >= 1000) return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return String(count);
}

function formatReleaseDate(isoDate: string): string {
  const ts = Date.parse(isoDate);
  if (Number.isNaN(ts)) return 'Nov 19, 2026';
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(ts));
}

function deriveReleaseHighlight(tag: string, releaseName?: string): string {
  const version = tag.replace(/^v/i, '');
  if (releaseName) {
    // Strip boilerplate prefix like "Cairnmind 0.12.0", "v0.12.0:", or "Release 0.12.0 -"
    const stripped = sanitizeHeadlineText(
      releaseName
        .replace(new RegExp(`^(?:cairnmind\\s+|release\\s+)?v?${version.replace(/\./g, '\\.')}\\s*[:\\-–—·]?\\s*`, 'i'), '')
    );
    if (stripped.length >= 6) {
      return stripped.length > 52 ? `${stripped.slice(0, 49).replace(/\s+\S*$/, '')}…` : stripped;
    }
  }
  return readChangelogHighlight(version) ?? 'Latest release';
}

export function getRepoStats(): Promise<RepoStats> {
  if (cached) return cached;

  cached = (async () => {
    const defaultTag = `v${REPO_META.version}`;
    const fallback: RepoStats = {
      stars: null,
      starsFormatted: null,
      releaseTag: defaultTag,
      releaseVersion: REPO_META.version,
      releaseHighlight: deriveReleaseHighlight(defaultTag),
      releasePublishedAt: SITE.releaseDateIso,
      releaseDateFormatted: formatReleaseDate(SITE.releaseDateIso),
    };

    if (process.env.SKIP_GITHUB_FETCH === '1') return fallback;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
      const headers: Record<string, string> = {
        Accept: 'application/vnd.github+json',
        'User-Agent': 'cairnmind-website-build',
      };
      if (process.env.GITHUB_TOKEN) {
        headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
      }

      const [repoRes, relRes] = await Promise.all([
        fetch('https://api.github.com/repos/piotrwozn/cairnmind', { headers, signal: controller.signal }),
        fetch('https://api.github.com/repos/piotrwozn/cairnmind/releases/latest', { headers, signal: controller.signal }),
      ]);

      let stars: number | null = null;
      if (repoRes.ok) {
        const data = (await repoRes.json()) as { stargazers_count?: number };
        if (typeof data.stargazers_count === 'number') stars = data.stargazers_count;
      }

      let releaseTag = fallback.releaseTag;
      let releaseName: string | undefined;
      let publishedAt = fallback.releasePublishedAt;
      if (relRes.ok) {
        const rel = (await relRes.json()) as { tag_name?: string; name?: string; published_at?: string };
        if (rel.tag_name) releaseTag = rel.tag_name;
        if (rel.name) releaseName = rel.name;
        if (rel.published_at) publishedAt = rel.published_at;
      }

      const releaseVersion = releaseTag.replace(/^v/i, '');
      return {
        stars,
        // Show numeric count automatically as soon as the repo gathers initial public stars (>= 5)
        starsFormatted: stars !== null && stars >= 5 ? formatStars(stars) : null,
        releaseTag,
        releaseVersion,
        releaseHighlight: deriveReleaseHighlight(releaseTag, releaseName),
        releasePublishedAt: publishedAt,
        releaseDateFormatted: formatReleaseDate(publishedAt),
      };
    } catch {
      return fallback;
    } finally {
      clearTimeout(timer);
    }
  })();

  return cached;
}
