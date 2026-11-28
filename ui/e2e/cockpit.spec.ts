import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

interface ServerInfo {
  bootstrapUrl: string;
  baseUrl: string;
  projectId: string;
  proposedDecisionId: string;
  confirmedDecisionId: string;
}

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, '..', '..');

function resolvePythonCommand(): { cmd: string; prefixArgs: string[] } {
  if (process.env.CAIRNMIND_E2E_PYTHON) {
    return { cmd: process.env.CAIRNMIND_E2E_PYTHON, prefixArgs: [] };
  }
  const winVenv = path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe');
  if (fs.existsSync(winVenv)) {
    return { cmd: winVenv, prefixArgs: [] };
  }
  const posixVenv = path.join(REPO_ROOT, '.venv', 'bin', 'python');
  if (fs.existsSync(posixVenv)) {
    return { cmd: posixVenv, prefixArgs: [] };
  }
  return { cmd: 'uv', prefixArgs: ['run', 'python'] };
}

async function startRealCairnmindUi(): Promise<{
  info: ServerInfo;
  stop: () => Promise<void>;
}> {
  const { cmd, prefixArgs } = resolvePythonCommand();
  const helper = path.join(__dirname, 'seed_and_serve.py');
  const child: ChildProcessWithoutNullStreams = spawn(cmd, [...prefixArgs, helper], {
    cwd: REPO_ROOT,
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  const stderrChunks: string[] = [];
  child.stderr.on('data', (chunk: Buffer) => stderrChunks.push(chunk.toString('utf-8')));

  const info = await new Promise<ServerInfo>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`Timed out starting cairnmind ui helper. stderr:\n${stderrChunks.join('')}`));
    }, 30_000);

    const rl = readline.createInterface({ input: child.stdout });
    rl.on('line', (line) => {
      if (line.startsWith('E2E_READY ')) {
        clearTimeout(timer);
        rl.close();
        try {
          resolve(JSON.parse(line.slice('E2E_READY '.length)) as ServerInfo);
        } catch (err) {
          reject(err);
        }
      }
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      rl.close();
      reject(
        new Error(
          `cairnmind ui helper exited early with code ${String(code)}. stderr:\n${stderrChunks.join('')}`,
        ),
      );
    });
  });

  const stop = async () => {
    if (child.exitCode === null) {
      child.stdin.end();
      await new Promise<void>((resolve) => {
        const killTimer = setTimeout(() => {
          child.kill();
          resolve();
        }, 5_000);
        child.once('exit', () => {
          clearTimeout(killTimer);
          resolve();
        });
      });
    }
  };

  return { info, stop };
}

let server: { info: ServerInfo; stop: () => Promise<void> } | null = null;

test.beforeAll(async () => {
  server = await startRealCairnmindUi();
});

test.afterAll(async () => {
  if (server) {
    await server.stop();
    server = null;
  }
});

test('real cairnmind ui auth bootstrap, HttpOnly+CSRF, SSE stream, mutation, and person_proof gate', async ({
  browser,
  request,
}) => {
  if (!server) throw new Error('E2E server did not start');
  const { bootstrapUrl, baseUrl, proposedDecisionId, confirmedDecisionId } = server.info;

  // 1. Unauthenticated API call is strictly rejected with 401
  const unauthResp = await request.get(`${baseUrl}/api/ui/snapshot`);
  expect(unauthResp.status()).toBe(401);

  // 2. Hostile cross-origin request is rejected with 403
  const crossOriginResp = await request.get(`${baseUrl}/api/ui/snapshot`, {
    headers: { Origin: 'https://evil.example' },
  });
  expect(crossOriginResp.status()).toBe(403);

  const context = await browser.newContext();
  const page = await context.newPage();
  const pageErrors: string[] = [];
  page.on('pageerror', (err) => pageErrors.push(err.message));

  try {
    // 3. Visit /auth/bootstrap?token=... -> redirects to / and sets HttpOnly session + Strict CSRF cookies
    await page.goto(bootstrapUrl, { waitUntil: 'domcontentloaded' });
    expect(page.url()).toBe(`${baseUrl}/`);

    const cookies = await context.cookies(baseUrl);
    const sessionCookie = cookies.find((c) => c.name === 'cairnmind_ui_session');
    const csrfCookie = cookies.find((c) => c.name === 'cairnmind_ui_csrf');
    expect(sessionCookie).toBeDefined();
    expect(sessionCookie?.httpOnly).toBe(true);
    expect(sessionCookie?.sameSite).toBe('Strict');
    expect(csrfCookie).toBeDefined();
    expect(csrfCookie?.httpOnly).toBe(false);
    expect(csrfCookie?.sameSite).toBe('Strict');

    // 4. Compiled Vite SPA hydrates real SQLite project state and connects SSE /api/ui/events
    await expect(page.getByText('e2e-demo')).toBeVisible();
    await expect(page.getByText('Live Event Stream')).toBeVisible();
    await expect(page.getByText(/connected state_rev=#/)).toBeVisible();

    // 5. Cookie-authenticated mutation WITHOUT X-Cairnmind-UI-Token CSRF header is blocked (403)
    const missingCsrfStatus = await page.evaluate(async () => {
      const resp = await fetch('/api/ui/actions/reverify', {
        method: 'POST',
        credentials: 'same-origin',
      });
      return resp.status;
    });
    expect(missingCsrfStatus).toBe(403);

    // 6. Authenticated mutation WITH X-Cairnmind-UI-Token succeeds and streams SSE state.updated
    await page.getByRole('button', { name: /Re-verify/i }).click();
    await expect(page.getByText(/Re-verified \d+ files/)).toBeVisible();

    // 7. Create a fact via authenticated CSRF request -> without person_proof it MUST remain proposed
    const factResult = await page.evaluate(async (csrfToken) => {
      const resp = await fetch('/api/ui/facts', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-Cairnmind-UI-Token': csrfToken,
        },
        body: JSON.stringify({
          kind: 'fact',
          key: 'e2e.ui.fact',
          title: 'Created from browser E2E',
          content: 'Browser session must never bypass person_proof',
          source_path: 'app.py',
          line_start: 1,
          line_end: 1,
          verbatim_quote: 'REQUEST_TIMEOUT = 30',
        }),
      });
      return { status: resp.status, body: await resp.json() };
    }, csrfCookie!.value);
    expect(factResult.status).toBe(200);
    expect(factResult.body.claim.status).toBe('proposed');

    // 8. Attempting to decide a CONFIRMED decision without workspace person_proof is refused (403)
    const personProofBlock = await page.evaluate(
      async ({ csrfToken, decisionId }) => {
        const resp = await fetch(`/api/ui/detected-decisions/${decisionId}/decide`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Cairnmind-UI-Token': csrfToken,
          },
          body: JSON.stringify({ action: 'ignore' }),
        });
        return { status: resp.status, body: await resp.json() };
      },
      { csrfToken: csrfCookie!.value, decisionId: confirmedDecisionId },
    );
    expect(personProofBlock.status).toBe(403);

    // 9. Accepting a PROPOSED decision without person_proof keeps status='proposed' & flags requires_person_confirmation
    const proposedDecisionResult = await page.evaluate(
      async ({ csrfToken, decisionId }) => {
        const resp = await fetch(`/api/ui/detected-decisions/${decisionId}/decide`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Cairnmind-UI-Token': csrfToken,
          },
          body: JSON.stringify({ action: 'accept' }),
        });
        return { status: resp.status, body: await resp.json() };
      },
      { csrfToken: csrfCookie!.value, decisionId: proposedDecisionId },
    );
    expect(proposedDecisionResult.status).toBe(200);
    expect(proposedDecisionResult.body.decision.record_status).toBe('proposed');
    expect(proposedDecisionResult.body.decision.requires_person_confirmation).toBe(true);

    // 10. Navigate across lazily-loaded views cleanly with zero runtime/page errors
    await page.getByRole('button', { name: /Provenance & Quotes/i }).click();
    await expect(page.getByText(/Default request timeout is 30 seconds|e2e\.proposed/i).first()).toBeVisible();
    await page.getByRole('button', { name: /Blast-Radius Graph/i }).click();
    await page.getByRole('button', { name: /Guardrail Audit/i }).click();
    await page.getByRole('button', { name: /Draft State Manager/i }).click();

    expect(pageErrors).toEqual([]);
  } finally {
    await context.close();
  }
});
