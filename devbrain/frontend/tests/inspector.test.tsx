import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Inspector } from '../src/Inspector';
const project = { id: 'project-a', name: 'A', root: '/projects/a' };
const record = {
  id: 'record-a',
  kind: 'decision',
  content: 'Use explicit native permissions.',
  status: 'proposed',
  version: 3,
  source_ids: ['source-a'],
  metadata: {},
};
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
function respond(value: unknown) {
  return new Response(JSON.stringify(value), { status: 200 });
}
describe('project memory inspector', () => {
  it('uses record CAS and reviews confidence without pretending to complete tasks', async () => {
    const mutations: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string, init: RequestInit) => {
        if (path.includes('/review')) {
          mutations.push(JSON.parse(String(init.body)));
          return respond({ record: { ...record, status: 'confirmed' } });
        }
        if (path.includes('/records/record-a')) return respond({ record });
        if (path.includes('/records?')) return respond({ items: [record], next_cursor: null });
        return respond({ items: [] });
      }),
    );
    render(<Inspector project={project} worktree={project.root} refresh={0} onChange={() => {}} />);
    fireEvent.click(await screen.findByText(record.content));
    fireEvent.change(await screen.findByRole('textbox', { name: 'Uzasadnienie oceny ustalenia' }), {
      target: { value: 'Matches the cited source.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Potwierdź' }));
    await waitFor(() =>
      expect(mutations).toEqual([
        {
          project: 'project-a',
          action: 'confirm',
          expected_version: 3,
          reason: 'Matches the cited source.',
        },
      ]),
    );
  });
  it('renders raw source content as text rather than HTML and reads it in bounded pages', async () => {
    const paths: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string) => {
        paths.push(path);
        if (path.includes('/sources/source-a'))
          return respond({
            source: { id: 'source-a', uri: 'conversation://a' },
            text: '<img src=x onerror="alert(1)">',
            next_offset: null,
          });
        if (path.includes('/records/record-a')) return respond({ record });
        return respond({ items: [record] });
      }),
    );
    const { container } = render(
      <Inspector project={project} worktree={project.root} refresh={0} onChange={() => {}} />,
    );
    fireEvent.click(await screen.findByText(record.content));
    fireEvent.click(await screen.findByText('source-a'));
    expect(await screen.findByText('<img src=x onerror="alert(1)">')).toBeTruthy();
    expect(container.querySelector('img')).toBeNull();
    expect(
      paths.some((path) => path.includes('/sources/source-a') && path.includes('limit=65536')),
    ).toBe(true);
  });
  it('never lets a delayed list from a previous project replace current project data', async () => {
    let resolveOld: (value: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string) => {
        if (path.includes('project=project-a'))
          return new Promise<Response>((resolve) => {
            resolveOld = resolve;
          });
        return respond({ items: [{ ...record, id: 'record-b', content: 'Current project B.' }] });
      }),
    );
    const view = render(
      <Inspector project={project} worktree={project.root} refresh={0} onChange={() => {}} />,
    );
    view.rerender(
      <Inspector
        project={{ id: 'project-b', name: 'B', root: '/projects/b' }}
        worktree="/projects/b"
        refresh={0}
        onChange={() => {}}
      />,
    );
    expect(await screen.findByText('Current project B.')).toBeTruthy();
    resolveOld(respond({ items: [record] }));
    await waitFor(() => expect(screen.queryByText(record.content)).toBeNull());
  });
});

it('shows queue failures through read-only projection routes', async () => {
  const calls: Array<{ path: string; method: string }> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init: RequestInit) => {
      calls.push({ path, method: init.method ?? 'GET' });
      if (path.includes('/jobs/job-a'))
        return respond({
          item: {
            id: 'job-a',
            kind: 'extract',
            state: 'failed',
            error_code: 'fixture_extract_failure',
          },
        });
      if (path.includes('/jobs?'))
        return respond({
          items: [
            {
              id: 'job-a',
              kind: 'extract',
              state: 'failed',
              attempts: 2,
              error_code: 'fixture_extract_failure',
            },
          ],
          next_cursor: null,
        });
      return respond({ items: [] });
    }),
  );
  render(<Inspector project={project} worktree={project.root} refresh={0} onChange={() => {}} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Kolejka' }));
  fireEvent.click(await screen.findByText('extract'));
  await waitFor(() =>
    expect(screen.getByText(/"error_code": "fixture_extract_failure"/)).toBeTruthy(),
  );
  expect(
    calls.filter((item) => item.path.includes('/jobs')).every((item) => item.method === 'GET'),
  ).toBe(true);
});
