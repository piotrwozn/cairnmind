import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Resvg } from '@resvg/resvg-js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_PATH = path.resolve(__dirname, '..', 'public', 'og-image.png');

const svg = `
<svg width="1200" height="630" viewBox="0 0 1200 630" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="g1" cx="18%" cy="8%" r="65%">
      <stop offset="0%" stop-color="#1e4a78" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#060709" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="g2" cx="88%" cy="15%" r="55%">
      <stop offset="0%" stop-color="#87afd7" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="#060709" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="card" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#1a1f28" stop-opacity="0.92"/>
      <stop offset="100%" stop-color="#101319" stop-opacity="0.96"/>
    </linearGradient>
    <pattern id="grid" width="48" height="48" patternUnits="userSpaceOnUse">
      <path d="M 48 0 L 0 0 0 48" fill="none" stroke="#ffffff" stroke-opacity="0.04" stroke-width="1"/>
    </pattern>
  </defs>

  <rect width="1200" height="630" fill="#060709"/>
  <rect width="1200" height="630" fill="url(#g1)"/>
  <rect width="1200" height="630" fill="url(#g2)"/>
  <rect width="1200" height="630" fill="url(#grid)"/>

  <!-- Main Obsidian Glass Card -->
  <rect x="64" y="56" width="1072" height="518" rx="24" fill="url(#card)" stroke="#ffffff" stroke-opacity="0.14" stroke-width="1.5"/>
  <line x1="88" y1="57" x2="1112" y2="57" stroke="#ffffff" stroke-opacity="0.22" stroke-width="1"/>

  <!-- Cairn geometric mark -->
  <rect x="116" y="136" width="38" height="9" rx="4.5" fill="#f1f5f9"/>
  <rect x="122" y="123" width="26" height="8.5" rx="4.25" fill="#87afd7"/>
  <rect x="128" y="110" width="14" height="8" rx="4" fill="#dadee3" fill-opacity="0.78"/>

  <text x="172" y="138" fill="#f1f5f9" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="30" font-weight="700">Cairnmind</text>

  <!-- Badges -->
  <rect x="850" y="108" width="234" height="36" rx="18" fill="#1e4a78" fill-opacity="0.38" stroke="#87afd7" stroke-opacity="0.45"/>
  <text x="967" y="131" text-anchor="middle" fill="#a9cbe9" font-family="Consolas, monospace" font-size="14" font-weight="600">OPEN SOURCE · APACHE-2.0</text>

  <!-- Main Headline -->
  <text x="116" y="230" fill="#f1f5f9" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="45" font-weight="700">Your coding agent says “Done”.</text>
  <text x="116" y="284" fill="#a9cbe9" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="45" font-weight="700">Cairnmind makes it prove it.</text>
  <text x="116" y="336" fill="#98a4b4" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="20" font-weight="400">Test gates, verbatim source quotes &amp; 12-language code graph for Claude Code, Codex &amp; Cursor.</text>

  <!-- 3 Evidence Pillars -->
  <rect x="116" y="388" width="308" height="128" rx="14" fill="#0c0f14" stroke="#f47067" stroke-opacity="0.40"/>
  <text x="140" y="424" fill="#f47067" font-family="Consolas, monospace" font-size="14" font-weight="700">STALE TEST GATE</text>
  <text x="140" y="458" fill="#f1f5f9" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="22" font-weight="700">BLOCKED on edit</text>
  <text x="140" y="488" fill="#98a4b4" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="15">Fresh SHA-256 test proof required</text>

  <rect x="446" y="388" width="308" height="128" rx="14" fill="#0c0f14" stroke="#8fd08a" stroke-opacity="0.34"/>
  <text x="470" y="424" fill="#8fd08a" font-family="Consolas, monospace" font-size="14" font-weight="700">VERBATIM OR UNKNOWN</text>
  <text x="470" y="458" fill="#f1f5f9" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="22" font-weight="700">Refuses to guess</text>
  <text x="470" y="488" fill="#98a4b4" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="15">Cites your repo or says “unknown”</text>

  <rect x="776" y="388" width="308" height="128" rx="14" fill="#0c0f14" stroke="#87afd7" stroke-opacity="0.38"/>
  <text x="800" y="424" fill="#87afd7" font-family="Consolas, monospace" font-size="14" font-weight="700">15-SECOND SANDBOX</text>
  <text x="800" y="458" fill="#f1f5f9" font-family="Consolas, monospace" font-size="21" font-weight="700">uvx cairnmind try</text>
  <text x="800" y="488" fill="#98a4b4" font-family="Inter, Segoe UI, Arial, sans-serif" font-size="15">No account · No GPU · Zero files</text>
</svg>
`;

const resvg = new Resvg(svg, {
  fitTo: { mode: 'width', value: 1200 },
  font: { loadSystemFonts: true },
});
const pngData = resvg.render();
const pngBuffer = pngData.asPng();
fs.writeFileSync(OUT_PATH, pngBuffer);
console.log(`[generate-og] wrote ${OUT_PATH} (${pngBuffer.length} bytes, ${pngData.width}x${pngData.height})`);
