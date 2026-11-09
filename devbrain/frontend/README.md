# DevBrain cockpit frontend

React/TypeScript UI for the local Python cockpit. Production uses only the bundled assets in `src/devbrain/web_static/`; end users do not need Node or npm.

Building and running frontend tests requires Node.js 20, 22, or 24+ (validated with Node.js 22.14.0).

```bash
npm ci
npm run test
npm run test:browser
npm run build
```

The browser tests use explicit API/WebSocket fixtures and `/usr/bin/chromium`. Set `CHROMIUM_PATH` to use another Chromium executable. They make no native paid model calls. A separate full-stack integration check should use the real Python HTTP/WebSocket server and managed tmux runtime.

`npm run dev` proxies `/api` and `/auth` to the local cockpit on port 8766. Production resources have no CDN or external font dependencies. Keep `package-lock.json` and built text assets with release changes; omit `node_modules`, test artifacts and TypeScript caches.

Browser bootstrap exchanges a single-use fragment ticket for an HttpOnly session and in-memory CSRF. Native terminal transport uses the authenticated run ID and generation. Do not store credentials, terminal transcripts or internal service bearer tokens in browser storage. Native authentication terminals are operational-only.

The workspace restores existing terminal views on load. It starts or resumes a native process only after an explicit user action. Record and plan CAS versions are required for memory review; browsing projections must remain read-only.
