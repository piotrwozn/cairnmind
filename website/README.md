# Cairnmind Product Website & Documentation Portal (`website/`)

Static Astro + React product website and integrated `/docs/*` documentation portal for [Cairnmind](https://github.com/piotrwozn/cairnmind).

This directory is excluded from Python wheels and source distributions (`pyproject.toml`), so users installing Cairnmind via `uvx`, `uv tool install`, or `pip` never download website assets.

## Local development & preview

Requires Node.js 20+:

```bash
cd website
npm ci
npm run dev       # http://127.0.0.1:4321
```

Production build and automated SEO/a11y/link audit:

```bash
cd website
npm run build
npm run verify
npm run preview   # serves static dist/ at http://127.0.0.1:4321
```

To regenerate the Open Graph social card (`public/og-image.png`):

```bash
npm run generate:og
```

## Single source of truth

- **Documentation (`/docs/*`) and Changelog (`/docs/changelog/`)** are compiled directly from `../docs/*.md` and `../CHANGELOG.md` at build time (`src/lib/docs.ts`). Never duplicate docs markdown into `website/`.
- **Release tag, release date, release highlight, and GitHub stars** are pulled automatically at build time from the GitHub Releases API (`src/lib/github.ts`), falling back cleanly to `../pyproject.toml` and the matching release heading in `../CHANGELOG.md` (`src/lib/repoMeta.ts`).
- **Engineering scale counts** (Python modules, test suites, CI test functions, and frozen CLI/MCP fixtures) are counted directly from `../src/`, `../tests/`, and `../tests/fixtures/contracts/` at build time (`src/lib/repoMeta.ts`).
- **Install commands (`src/data/install.json`)**, **live `cairnmind try` output (`src/data/try-output.json`)**, and **measured figures (`src/data/benchmarks.json`)** are verified against `docs/INSTALL.md`, `README.md`, `src/cairnmind/try_demo.py`, `docs/EVALUATION.md`, and `docs/evaluation/RESULTS.md` by `tests/test_website_sync.py`.
- **Live contract explorer (`src/lib/contracts.ts`)** loads real frozen JSON fixtures directly from `../tests/fixtures/contracts/`.
- **Real captures (`public/media/`)** are synced from `../docs/assets/` by `scripts/sync-media.mjs`.

## Pre-launch gate (before public announcement)

Before connecting `cairnmind.dev` and announcing publicly (Show HN, Reddit, MCP registries):

1. Publish the release (`0.13.0` / `1.0.0`) containing `cairnmind try`, native Windows, SCIP, and Web UI to PyPI under the package name `cairnmind`.
2. Smoke-test from clean machines (Linux, Windows, macOS): `uvx cairnmind try --non-interactive` and `pipx run cairnmind try --non-interactive` must succeed.
3. Ensure `piotrwozn/cairnmind` is public so GitHub Star, Discussions, Good First Issues, Releases links, and the build-time GitHub API counter work for anonymous visitors.

## Cloudflare Pages deployment checklist

1. In Cloudflare Dashboard → **Workers & Pages** → **Create** → **Pages** → **Connect to Git** → select `piotrwozn/cairnmind`.
2. Build settings:
   - **Root directory:** `website`
   - **Build command:** `npm run build`
   - **Build output directory:** `dist`
   - **Node version:** `22` (`NODE_VERSION=22`)
   - **Watch paths:** `website/**`, `docs/**`, `CHANGELOG.md`, `pyproject.toml`, `tests/fixtures/contracts/**`
3. Environment variables:
   - Production: `SITE_URL=https://cairnmind.dev`
   - Preview: set `SITE_URL` to your `*.pages.dev` preview URL (builds whose `SITE_URL` is not `https://cairnmind.dev` automatically emit `<meta name="robots" content="noindex, nofollow">` to prevent duplicate indexing).
4. Optional zero-touch release & daily rebuilds:
   - In Cloudflare Pages → **Settings** → **Builds & deployments** → **Deploy hooks**, create a hook targeting `main` and save its URL in GitHub Repository Secrets as `CLOUDFLARE_DEPLOY_HOOK`.
   - `.github/workflows/website.yml` will automatically trigger a live rebuild on every published GitHub Release (`on: release: [published]`) and daily at 06:00 UTC (`schedule`) to refresh the release tag, date, changelog, and GitHub star badge.
5. Attach custom domain `cairnmind.dev` (and redirect `www.cairnmind.dev` → `cairnmind.dev`).
6. Submit `https://cairnmind.dev/sitemap-index.xml` in Google Search Console and Bing Webmaster Tools.
7. Once live, add the `https://cairnmind.dev` link to the root `README.md` and `pyproject.toml` (`Homepage`).
