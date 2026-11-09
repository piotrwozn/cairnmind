import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
const project = {
  id: 'project-a',
  name: 'Orbit API',
  root: '/home/piotr/src/orbit-api',
  roots: ['/home/piotr/src/orbit-api'],
};
const record = {
  id: 'decision-a',
  kind: 'decision',
  content: 'Keep authentication in secure server sessions.',
  status: 'confirmed',
  version: 2,
  source_ids: ['source-a'],
  metadata: {},
};
async function fixtures(page: Page, inactive = false, projects = [project]) {
  const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
  const runs = [
    {
      id: 'run-codex',
      generation: 1,
      project_id: project.id,
      worktree: project.root,
      client: 'codex',
      slot_id: 'codex',
      lifecycle: inactive ? 'stopped' : 'running',
      session_id: 'session-a',
      memory: { status: 'ready', acknowledged: false },
    },
    {
      id: 'run-claude',
      generation: 1,
      project_id: project.id,
      worktree: project.root,
      client: 'claude',
      slot_id: 'claude',
      lifecycle: inactive ? 'stopped' : 'running',
      session_id: 'session-b',
      memory: { status: 'ready', acknowledged: true },
    },
  ];
  await page.route('**/auth/session', (route) => route.fulfill({ json: { csrf: 'fixture-csrf' } }));
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname;
    const body = route.request().postDataJSON() ?? {};
    if (route.request().method() !== 'GET') requests.push({ path, body });
    if (path === '/api/state')
      return route.fulfill({
        json: {
          projects,
          profiles: [
            { id: 'codex', client: 'codex', label: 'Codex' },
            { id: 'claude', client: 'claude', label: 'Claude' },
          ],
          settings: {
            scan_roots: [],
            default_slots: [
              { id: 'codex', profile_id: 'codex' },
              { id: 'claude', profile_id: 'claude' },
            ],
            project_slots: {},
            last_project: project.id,
          },
          runs,
          memory_available: true,
        },
      });
    if (path === '/api/events')
      return route.fulfill({ contentType: 'text/event-stream', body: ': fixture keepalive\n\n' });
    if (path === '/api/auth')
      return route.fulfill({
        json: {
          installed: true,
          auth: url.searchParams.get('client') === 'claude' ? 'signed_out' : 'reported_signed_in',
          usage: 'unknown',
        },
      });
    if (path === '/api/client-runs')
      return route.fulfill({
        json: {
          runs: body.resume ? [] : runs,
          errors: body.resume
            ? [
                {
                  code: 'native_resume_unavailable',
                  message: 'Brak zweryfikowanego identyfikatora natywnej sesji.',
                },
              ]
            : [],
        },
      });
    if (path === '/api/inspector/records')
      return route.fulfill({
        json: {
          items: [
            record,
            {
              id: 'task-a',
              kind: 'task',
              content: 'Review token validation and capture fresh evidence.',
              status: 'proposed',
              version: 1,
              metadata: { state: 'needs_review' },
              source_ids: ['source-a'],
            },
          ],
          next_cursor: null,
          total: 2,
        },
      });
    if (path === '/api/inspector/records/decision-a') return route.fulfill({ json: { record } });
    if (path === '/api/inspector/sources/source-a')
      return route.fulfill({
        json: {
          source: {
            id: 'source-a',
            kind: 'transcript',
            uri: 'conversation://codex/native-a',
            sha256: 'fixture-sha',
          },
          text: '<script>window.__unsafe=true</script>\nUse server-side sessions.',
          next_offset: null,
        },
      });
    if (path === '/api/inspector/graph')
      return route.fulfill({
        json: {
          nodes: [
            { id: 'record:decision-a', kind: 'decision', label: 'Server-side sessions' },
            {
              id: 'source:source-a',
              kind: 'source',
              label: 'Session source',
              source_id: 'source-a',
            },
            { id: 'record:task-a', kind: 'task', label: 'Review token validation' },
          ],
          edges: [
            {
              id: 'edge-a',
              from_id: 'record:decision-a',
              to_id: 'source:source-a',
              kind: 'supported_by',
            },
            {
              id: 'edge-b',
              from_id: 'record:task-a',
              to_id: 'source:source-a',
              kind: 'supported_by',
            },
          ],
          groups: [],
          counts: { nodes: 3, edges: 2 },
          truncated: false,
        },
      });
    if (path === '/api/memory/import')
      return route.fulfill({
        json: {
          status: body.dry_run ? 'dry_run' : 'imported',
          project,
          verification: 'historical',
        },
      });
    return route.fulfill({ json: { items: [], status: 'ok' } });
  });
  await page.routeWebSocket('**/api/client-runs/*/terminal?*', (socket) => {
    socket.send(JSON.stringify({ type: 'control', controller: true }));
    socket.send(
      JSON.stringify({
        type: 'output',
        encoding: 'base64',
        data: Buffer.from(
          socket.url().includes('codex')
            ? 'OpenAI Codex\r\n\r\n> Ready in /home/piotr/src/orbit-api\r\n'
            : 'Claude Code\r\n\r\n❯ Review stage ready.\r\n',
        ).toString('base64'),
      }),
    );
  });
  return requests;
}
test('native panels keep truthful ACK badges and source text stays inert', async ({ page }) => {
  await fixtures(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Orbit API' })).toBeVisible();
  await expect(page.getByText('ACK wymagany')).toBeVisible();
  await expect(page.getByText('ACK potwierdzony')).toBeVisible();
  await page.getByText(record.content).click();
  await page.getByRole('button', { name: 'source-a' }).click();
  await expect(page.locator('.source-text')).toContainText('<script>window.__unsafe=true</script>');
  expect(await page.evaluate(() => '__unsafe' in window)).toBe(false);
  await page.screenshot({ path: '/tmp/devbrain-cockpit-contract.png', fullPage: true });
});
test('snapshot import needs a matching dry run and keeps explicit destination', async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Wczytaj pamięć', exact: true }).first().click();
  const modal = page.getByRole('dialog');
  await modal.getByLabel('Snapshot lub manifest').fill('/projects/portable/.memory/devbrain');
  await modal.getByLabel('Katalog docelowego projektu').fill('/projects/portable');
  await expect(modal.getByRole('button', { name: 'Wczytaj pamięć', exact: true })).toBeDisabled();
  await modal.getByRole('button', { name: 'Sprawdź import' }).click();
  await expect(modal.getByRole('button', { name: 'Wczytaj pamięć', exact: true })).toBeEnabled();
  await modal.getByLabel('Lokalna nazwa (opcjonalna)').fill('Portable');
  await expect(modal.getByRole('button', { name: 'Wczytaj pamięć', exact: true })).toBeDisabled();
  await modal.getByRole('button', { name: 'Sprawdź import' }).click();
  await modal.getByRole('button', { name: 'Wczytaj pamięć', exact: true }).click();
  expect(requests.filter((item) => item.path === '/api/memory/import').at(-1)?.body).toMatchObject({
    root: '/projects/portable',
    name: 'Portable',
    dry_run: false,
  });
});

test('restoring a browser never starts paid sessions; explicit starts use fresh compact operation keys', async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Orbit API' })).toBeVisible();
  expect(requests.filter((item) => item.path === '/api/client-runs')).toHaveLength(0);
  await page.getByRole('button', { name: 'Połącz klientów' }).click();
  await expect
    .poll(() => requests.filter((item) => item.path === '/api/client-runs').length)
    .toBe(1);
  const first = requests.find((item) => item.path === '/api/client-runs')!.body.idempotency_key;
  expect(String(first)).toMatch(/^[a-f0-9-]{36}$/);
  await page.getByRole('button', { name: 'Połącz klientów' }).click();
  await expect
    .poll(() => requests.filter((item) => item.path === '/api/client-runs').length)
    .toBe(2);
  const second = requests.filter((item) => item.path === '/api/client-runs')[1].body
    .idempotency_key;
  expect(second).not.toBe(first);
});

test('stopped coding runs require explicit resume and offer a new session when native identity is unavailable', async ({
  page,
}) => {
  const requests = await fixtures(page, true);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Orbit API' })).toBeVisible();
  expect(requests.filter((item) => item.path === '/api/client-runs')).toHaveLength(0);
  await page.getByRole('button', { name: 'Wznów sesję', exact: true }).first().click();
  await expect(
    page.getByRole('status').filter({ hasText: 'native_resume_unavailable' }),
  ).toBeVisible();
  const resumed = requests.find((item) => item.path === '/api/client-runs')!.body;
  expect(resumed).toMatchObject({
    project: 'project-a',
    worktree: project.root,
    resume: true,
    slots: [{ id: 'codex', profile_id: 'codex' }],
  });
  await page.getByRole('button', { name: 'Nowa sesja', exact: true }).first().click();
  await expect
    .poll(() => requests.filter((item) => item.path === '/api/client-runs').length)
    .toBe(2);
  expect(requests.filter((item) => item.path === '/api/client-runs')[1].body).toMatchObject({
    resume: false,
  });
});

test('native profile model is an optional explicit override', async ({ page }) => {
  const requests = await fixtures(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Ustawienia środowiska' }).click();
  await page.getByLabel('Model profilu codex').fill('gpt-test-native');
  await page.getByRole('button', { name: 'Zapisz ustawienia' }).click();
  await expect.poll(() => requests.filter((item) => item.path === '/api/profiles').length).toBe(1);
  expect(
    (
      requests.find((item) => item.path === '/api/profiles')!.body.profiles as Array<
        Record<string, unknown>
      >
    )[0].model,
  ).toBe('gpt-test-native');
});

test('manual registration detects portable memory before creating an identity and never hides corruption', async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.route('**/api/projects/inspect', (route) =>
    route.fulfill({
      json: { root: '/projects/clone', memory_snapshot: '/projects/clone/.memory/devbrain' },
    }),
  );
  await page.route('**/api/memory/import', (route) =>
    route.fulfill({ status: 400, json: { detail: 'Corrupt memory source snapshot' } }),
  );
  await page.goto('/');
  await page.getByRole('button', { name: 'Dodaj projekt', exact: true }).first().click();
  const add = page.getByRole('dialog');
  await add.getByLabel('Nazwa projektu', { exact: true }).fill('Portable clone');
  await add.getByLabel('Katalog projektu', { exact: true }).fill('/projects/clone');
  await add.getByRole('button', { name: 'Dodaj projekt', exact: true }).click();
  await expect(add.getByText('W tym katalogu jest przenośna pamięć.')).toBeVisible();
  expect(requests.filter((item) => item.path === '/api/projects')).toHaveLength(0);
  await add.getByRole('button', { name: 'Sprawdź i wczytaj pamięć' }).click();
  const restore = page.getByRole('dialog');
  await expect(restore.getByLabel('Snapshot lub manifest')).toHaveValue(
    '/projects/clone/.memory/devbrain',
  );
  await expect(restore.getByLabel('Katalog docelowego projektu')).toHaveValue('/projects/clone');
  await restore.getByRole('button', { name: 'Sprawdź import' }).click();
  await expect(restore.getByRole('alert')).toContainText('Corrupt memory source snapshot');
  await expect(restore.getByRole('button', { name: 'Wczytaj pamięć', exact: true })).toBeDisabled();
  expect(requests.filter((item) => item.path === '/api/projects')).toHaveLength(0);
});

test('discovered clones offer explicit memory import instead of registering a new UUID', async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.route('**/api/discovery', (route) =>
    route.fulfill({
      json: {
        state: 'completed',
        roots: ['/projects'],
        candidates: [
          {
            name: 'Portable clone',
            root: '/projects/clone',
            memory_snapshot: '/projects/clone/.memory/devbrain',
          },
        ],
      },
    }),
  );
  await page.goto('/');
  await page
    .getByRole('complementary')
    .getByRole('button', { name: 'Znajdź projekty', exact: true })
    .click();
  const discovery = page.getByRole('dialog');
  await expect(discovery.getByText('Portable clone')).toBeVisible();
  await discovery.getByRole('button', { name: 'Wczytaj pamięć', exact: true }).click();
  const restore = page.getByRole('dialog');
  await expect(restore.getByLabel('Snapshot lub manifest')).toHaveValue(
    '/projects/clone/.memory/devbrain',
  );
  await expect(restore.getByLabel('Katalog docelowego projektu')).toHaveValue('/projects/clone');
  await expect(restore.getByRole('button', { name: 'Wczytaj pamięć', exact: true })).toBeDisabled();
  expect(requests.filter((item) => item.path === '/api/projects')).toHaveLength(0);
  expect(requests.filter((item) => item.path === '/api/memory/import')).toHaveLength(0);
});

test('global project graph drilldown changes inspection scope without launching native sessions', async ({
  page,
}) => {
  const other = { id: 'project-b', name: 'Project B', root: '/projects/b', roots: ['/projects/b'] };
  const requests = await fixtures(page, false, [project, other]);
  const graphScopes: string[] = [],
    sourceScopes: string[] = [],
    invalidGroupRequests: string[] = [];
  await page.route('**/api/inspector/graph/groups?*', (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.get('project')) invalidGroupRequests.push(url.pathname);
    return route.fulfill({ json: { items: [] } });
  });
  await page.route('**/api/inspector/graph?*', (route) => {
    const url = new URL(route.request().url()),
      scope = url.searchParams.get('project');
    if (scope) graphScopes.push(scope);
    const nodes = scope
      ? [
          {
            id: 'source:source-b',
            source_id: 'source-b',
            kind: 'source',
            label: 'Only project B source',
          },
        ]
      : [project, other].map((item) => ({
          id: 'project:' + item.id,
          kind: 'project',
          project_id: item.id,
          label: item.name,
        }));
    return route.fulfill({ json: { nodes, edges: [], groups: scope ? [] : nodes } });
  });
  await page.route('**/api/inspector/records?*', (route) => {
    const scope = new URL(route.request().url()).searchParams.get('project');
    return route.fulfill({
      json: {
        items:
          scope === 'project-b'
            ? [{ ...record, id: 'record-b', content: 'Only project B knowledge.' }]
            : [record],
      },
    });
  });
  await page.route('**/api/inspector/sources/source-b?*', (route) => {
    sourceScopes.push(new URL(route.request().url()).searchParams.get('project') ?? '');
    return route.fulfill({
      json: {
        source: { id: 'source-b', uri: 'note://b' },
        text: 'Project B exact source.',
        next_offset: null,
      },
    });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Orbit API' })).toBeVisible();
  await page.getByRole('button', { name: 'Pokaż wszystkie projekty' }).click();
  await page.locator('.graph-toolbar').getByRole('button', { name: 'Grupy', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: /Project B/ })
    .click();
  await expect(page.getByRole('heading', { name: /Project B/ })).toBeVisible();
  await expect(page.getByText('Only project B knowledge.')).toBeVisible();
  await expect.poll(() => graphScopes.at(-1)).toBe('project-b');
  await page.getByRole('button', { name: 'Widok listy' }).click();
  await page
    .locator('.graph-list')
    .getByRole('button', { name: /Only project B source/ })
    .click();
  await expect(page.locator('.source-text')).toHaveText('Project B exact source.');
  expect(sourceScopes).toEqual(['project-b']);
  await page.getByRole('button', { name: 'Pokaż wszystkie projekty' }).click();
  await page
    .locator('.graph-list')
    .getByRole('button', { name: /Orbit API/ })
    .click();
  await expect(page.getByRole('heading', { name: 'Orbit API' })).toBeVisible();
  await expect.poll(() => graphScopes.at(-1)).toBe('project-a');
  expect(invalidGroupRequests).toEqual([]);
  expect(requests.filter((item) => item.path === '/api/client-runs')).toHaveLength(0);
});
