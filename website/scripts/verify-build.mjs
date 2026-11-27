import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(__dirname, '..', 'dist');

const errors = [];
const warnings = [];

function walkHtml(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkHtml(full));
    else if (entry.name.endsWith('.html')) out.push(full);
  }
  return out;
}

function checkRequiredStaticFiles() {
  const mustExist = [
    'robots.txt',
    'llms.txt',
    'llms-full.txt',
    'sitemap-index.xml',
    'favicon.svg',
    'favicon.ico',
    'og-image.png',
    'search-index.json',
    '_headers',
    'media/demo-60s.svg',
    'media/try-demo.gif',
    'media/try-demo-poster.webp',
    'media/handoff-poster.webp',
    'media/handoff-demo.mp4',
    'media/handoff-demo.gif',
    'media/tui-workspace.svg',
    'media/author-avatar.webp',
  ];
  for (const rel of mustExist) {
    const abs = path.join(DIST, rel);
    if (!fs.existsSync(abs)) {
      errors.push(`Missing required dist file: ${rel}`);
    }
  }

  const robotsPath = path.join(DIST, 'robots.txt');
  if (fs.existsSync(robotsPath)) {
    const robots = fs.readFileSync(robotsPath, 'utf-8');
    for (const token of ['GPTBot', 'OAI-SearchBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended', 'sitemap-index.xml']) {
      if (!robots.includes(token)) {
        errors.push(`robots.txt missing required directive/token: ${token}`);
      }
    }
  }

  const llmsPath = path.join(DIST, 'llms.txt');
  if (fs.existsSync(llmsPath)) {
    const llms = fs.readFileSync(llmsPath, 'utf-8');
    if (!llms.includes('https://github.com/piotrwozn/cairnmind') || !llms.includes('/docs/')) {
      errors.push('llms.txt missing GitHub or /docs/ links');
    }
  }

  const searchIdxPath = path.join(DIST, 'search-index.json');
  if (fs.existsSync(searchIdxPath)) {
    const parsed = JSON.parse(fs.readFileSync(searchIdxPath, 'utf-8'));
    if (!Array.isArray(parsed) || parsed.length < 20) {
      errors.push(`search-index.json too small (${Array.isArray(parsed) ? parsed.length : 'invalid'})`);
    }
  }
}

function resolveDistTarget(href) {
  const clean = href.split('#')[0].split('?')[0];
  if (!clean || clean === '/') return path.join(DIST, 'index.html');
  const rel = clean.replace(/^\/+/, '');
  const direct = path.join(DIST, rel);
  if (fs.existsSync(direct) && fs.statSync(direct).isFile()) return direct;
  const asIndex = path.join(DIST, rel, 'index.html');
  if (fs.existsSync(asIndex)) return asIndex;
  const asHtml = path.join(DIST, `${rel}.html`);
  if (fs.existsSync(asHtml)) return asHtml;
  return null;
}

if (!fs.existsSync(DIST)) {
  console.error('dist/ does not exist. Run `npm run build` first.');
  process.exit(1);
}

checkRequiredStaticFiles();

const htmlFiles = walkHtml(DIST);
const seenTitles = new Map();
const seenDescriptions = new Map();
let totalHtmlBytes = 0;

for (const file of htmlFiles) {
  const rel = path.relative(DIST, file).replace(/\\/g, '/');
  const html = fs.readFileSync(file, 'utf-8');
  totalHtmlBytes += Buffer.byteLength(html, 'utf-8');

  if (!/<html[^>]*\blang="en"/i.test(html)) {
    errors.push(`${rel}: missing <html lang="en">`);
  }

  if (!html.includes('href="#main-content"')) {
    errors.push(`${rel}: missing skip-to-content link`);
  }

  const h1Matches = [...html.matchAll(/<h1\b[^>]*>/gi)];
  if (h1Matches.length !== 1) {
    errors.push(`${rel}: expected exactly 1 <h1>, found ${h1Matches.length}`);
  }

  // Title & description checks (including SERP length discipline on product/SEO pages)
  const titleMatch = html.match(/<title>([^<]+)<\/title>/i);
  if (!titleMatch || !titleMatch[1].trim()) {
    errors.push(`${rel}: missing <title>`);
  } else {
    const t = titleMatch[1].trim();
    if (seenTitles.has(t)) {
      errors.push(`${rel}: duplicate <title> "${t}" (also on ${seenTitles.get(t)})`);
    }
    seenTitles.set(t, rel);
    if (!rel.startsWith('docs/') && rel !== '404.html' && t.length > 62) {
      errors.push(`${rel}: <title> exceeds 62 chars (${t.length}): "${t}"`);
    }
  }

  const descMatch = html.match(/<meta\s+name="description"\s+content="([^"]+)"/i);
  if (!descMatch || descMatch[1].trim().length < 25) {
    errors.push(`${rel}: missing or too short <meta name="description">`);
  } else if (rel !== '404.html') {
    const d = descMatch[1].trim();
    if (seenDescriptions.has(d)) {
      warnings.push(`${rel}: duplicate meta description with ${seenDescriptions.get(d)}`);
    }
    seenDescriptions.set(d, rel);
    if (!rel.startsWith('docs/') && (d.length < 110 || d.length > 162)) {
      errors.push(`${rel}: <meta description> outside 110..162 SERP range (${d.length})`);
    }
  }

  for (const requiredTag of [
    /<link\s+rel="canonical"\s+href="https?:\/\/[^"]+"/i,
    /<meta\s+property="og:title"\s+content="[^"]+"/i,
    /<meta\s+property="og:description"\s+content="[^"]+"/i,
    /<meta\s+property="og:image"\s+content="[^"]+"/i,
    /<meta\s+name="twitter:card"\s+content="summary_large_image"/i,
  ]) {
    if (!requiredTag.test(html)) {
      errors.push(`${rel}: missing required SEO/OG tag matching ${requiredTag}`);
    }
  }

  // Validate JSON-LD blocks
  const jsonLdBlocks = [...html.matchAll(/<script\s+type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/gi)];
  const ldTypes = new Set();
  for (const block of jsonLdBlocks) {
    try {
      const parsed = JSON.parse(block[1]);
      if (parsed['@type']) ldTypes.add(parsed['@type']);
    } catch (err) {
      errors.push(`${rel}: invalid JSON-LD block (${err.message})`);
    }
  }
  if (rel === 'index.html' && !ldTypes.has('SoftwareApplication')) {
    errors.push('index.html: missing SoftwareApplication JSON-LD');
  }
  if (rel === 'faq/index.html' && !ldTypes.has('FAQPage')) {
    errors.push('faq/index.html: missing FAQPage JSON-LD');
  }

  // Check images have non-empty alt
  for (const img of html.matchAll(/<img\b([^>]*)>/gi)) {
    const attrs = img[1];
    const altMatch = attrs.match(/\balt="([^"]*)"/i);
    if (!altMatch || !altMatch[1].trim()) {
      errors.push(`${rel}: <img> missing non-empty alt attribute`);
    }
  }

  // Check for empty href="" or src=""
  if (/\b(?:href|src)=""/i.test(html)) {
    errors.push(`${rel}: contains empty href="" or src="" attribute`);
  }

  // Check internal href and src resolve in dist/
  for (const link of html.matchAll(/\b(?:href|src)="(\/[^"]*)"/gi)) {
    const target = link[1];
    if (target.startsWith('//')) continue;
    if (!resolveDistTarget(target)) {
      errors.push(`${rel}: broken internal target "${target}"`);
    }
  }

  const stripEntities = (s) => s.replace(/&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);/gi, '');
  const cleanedHtml = html
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
    .replace(/<pre\b[^>]*>[\s\S]*?<\/pre>/gi, '')
    .replace(/<code\b[^>]*>[\s\S]*?<\/code>/gi, '');
  const visibleText = stripEntities(cleanedHtml.replace(/<[^>]+>/g, ' '));

  // Enforce zero em-dash across EVERY page (including /docs/*)
  const emIdx = visibleText.indexOf('—');
  if (emIdx !== -1) {
    const snippet = visibleText.slice(Math.max(0, emIdx - 32), emIdx + 40).replace(/\s+/g, ' ').trim();
    errors.push(`${rel}: visible prose contains forbidden em-dash (—): "...${snippet}..."`);
  }

  // Enforce strict heading hierarchy & zero semicolon prose on non-docs authored pages
  if (!rel.startsWith('docs/')) {
    const depths = [...html.matchAll(/<h([1-6])\b/gi)].map((m) => Number(m[1]));
    for (let i = 1; i < depths.length; i += 1) {
      if (depths[i] > depths[i - 1] + 1) {
        errors.push(`${rel}: heading hierarchy skips from h${depths[i - 1]} to h${depths[i]}`);
        break;
      }
    }

    const semiIdx = visibleText.indexOf(';');
    if (semiIdx !== -1) {
      const snippet = visibleText.slice(Math.max(0, semiIdx - 32), semiIdx + 40).replace(/\s+/g, ' ').trim();
      errors.push(`${rel}: visible prose contains forbidden semicolon (;): "...${snippet}..."`);
    }

    // Check alt, title, aria-label, and SEO/OG meta content attributes for em-dash or semicolon
    const attrRegex = /\b(?:alt|title|aria-label)="([^"]+)"/gi;
    for (const m of cleanedHtml.matchAll(attrRegex)) {
      const val = stripEntities(m[1]);
      if (val.includes('—') || val.includes(';')) {
        errors.push(`${rel}: attribute contains forbidden em-dash or semicolon: ${m[0].slice(0, 90)}`);
      }
    }
    const metaRegex = /<meta\s+(?:name|property)="(?:description|og:title|og:description|og:image:alt|twitter:title|twitter:description)"\s+content="([^"]+)"/gi;
    for (const m of html.matchAll(metaRegex)) {
      const val = stripEntities(m[1]);
      if (val.includes('—') || val.includes(';')) {
        errors.push(`${rel}: meta tag contains forbidden em-dash or semicolon: ${m[0].slice(0, 100)}`);
      }
    }
  }
}

// Measure JS asset footprint
const astroDir = path.join(DIST, '_astro');
let jsBytes = 0;
let jsCount = 0;
if (fs.existsSync(astroDir)) {
  for (const name of fs.readdirSync(astroDir)) {
    if (name.endsWith('.js')) {
      jsCount += 1;
      jsBytes += fs.statSync(path.join(astroDir, name)).size;
    }
  }
}

console.log(
  `[verify-build] audited ${htmlFiles.length} HTML pages (${(totalHtmlBytes / 1024).toFixed(1)} KB HTML total), ${jsCount} JS chunks (${(jsBytes / 1024).toFixed(1)} KB uncompressed).`
);

if (warnings.length > 0) {
  console.warn(`[verify-build] ${warnings.length} warning(s):`);
  for (const w of warnings) console.warn(`  WARN: ${w}`);
}

if (errors.length > 0) {
  console.error(`[verify-build] FAILED with ${errors.length} error(s):`);
  for (const e of errors) console.error(`  ERR: ${e}`);
  process.exit(1);
}

console.log('[verify-build] PASS — SEO, JSON-LD, links, AI crawler files, and a11y checks all green.');
