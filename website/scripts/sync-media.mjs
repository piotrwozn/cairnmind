import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEBSITE_ROOT = path.resolve(__dirname, '..');
const REPO_ASSETS = path.resolve(WEBSITE_ROOT, '..', 'docs', 'assets');
const PUBLIC_MEDIA = path.join(WEBSITE_ROOT, 'public', 'media');

const FILES = ['demo-60s.svg', 'handoff-demo.gif'];

fs.mkdirSync(PUBLIC_MEDIA, { recursive: true });

for (const name of FILES) {
  const src = path.join(REPO_ASSETS, name);
  const dst = path.join(PUBLIC_MEDIA, name);
  if (!fs.existsSync(src)) {
    throw new Error(`Missing canonical repo media asset: ${src}`);
  }
  const srcBuf = fs.readFileSync(src);
  const needsCopy = !fs.existsSync(dst) || !fs.readFileSync(dst).equals(srcBuf);
  if (needsCopy) {
    fs.writeFileSync(dst, srcBuf);
    console.log(`[sync-media] synced ${name} (${srcBuf.length} bytes)`);
  }
}
