import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  ChevronRight,
  FileText,
  GitBranch,
  History,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from 'lucide-react';
import { api, message, post, query } from './api';
import {
  Badge,
  Busy,
  Empty,
  formatTime,
  IconButton,
  label,
  object,
  entries,
  SmallData,
} from './components';
import type { GraphNode, Json, Project, RecordItem, SourceItem } from './types';
const names: Record<string, string> = {
  decision: 'Decyzja',
  task: 'Zadanie',
  fact: 'Ustalenie',
  constraint: 'Ograniczenie',
  question: 'Pytanie',
  preference: 'Preferencja',
  checkpoint: 'Checkpoint',
  proposed: 'Propozycja',
  confirmed: 'Potwierdzone',
  disputed: 'Kwestionowane',
  superseded: 'Historyczne',
  todo: 'Do zrobienia',
  in_progress: 'W toku',
  needs_test: 'Do testów',
  needs_review: 'Do review',
  done: 'Ukończone',
  blocked: 'Zablokowane',
};
const tone = (status: string) =>
  status === 'confirmed' || status === 'fresh'
    ? 'good'
    : status === 'disputed' || status === 'stale'
      ? 'warn'
      : 'muted';
interface Page<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number;
}
export function MemoryImportForm({
  root = '',
  initialPath = '',
  onDone,
}: {
  root?: string;
  initialPath?: string;
  onDone: (result?: Json) => void;
}) {
  const [path, setPath] = useState(initialPath),
    [destination, setDestination] = useState(root),
    [name, setName] = useState(''),
    [result, setResult] = useState<Json | null>(null),
    [ready, setReady] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const run = async (dry: boolean) => {
    setBusy(true);
    setError('');
    try {
      const value = await post<Json>('/api/memory/import', {
        path,
        root: destination,
        ...(name ? { name } : {}),
        dry_run: dry,
      });
      setResult(value);
      setReady(dry && ['dry_run', 'unchanged'].includes(String(value.status)));
      if (!dry) onDone(value);
    } catch (err) {
      setError(message(err));
      setReady(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="import-form">
      <p className="hint">
        Przeniesiesz pamięć jednego projektu. Pozostałe projekty pozostaną bez zmian. Wyniki testów
        z innego komputera będą historyczne.
      </p>
      <label>
        Snapshot lub manifest
        <input
          value={path}
          placeholder="/home/…/projekt/.memory/devbrain"
          onChange={(e) => {
            setPath(e.target.value);
            setReady(false);
          }}
        />
      </label>
      <label>
        Katalog docelowego projektu
        <input
          value={destination}
          placeholder="/home/…/projekt"
          onChange={(e) => {
            setDestination(e.target.value);
            setReady(false);
          }}
        />
      </label>
      <label>
        Lokalna nazwa (opcjonalna)
        <input
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setReady(false);
          }}
        />
      </label>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {result && <SmallData data={result} />}
      <div className="form-actions">
        <button
          className="button"
          disabled={!path || !destination || busy}
          onClick={() => void run(true)}
        >
          {busy ? <Busy /> : 'Sprawdź import'}
        </button>
        <button
          className="button primary"
          disabled={!ready || busy}
          onClick={() => void run(false)}
        >
          Wczytaj pamięć
        </button>
      </div>
    </div>
  );
}
function MemoryPanel({
  project,
  worktree,
  onChange,
}: {
  project: Project;
  worktree: string;
  onChange: () => void;
}) {
  const [status, setStatus] = useState<Json>({}),
    [preview, setPreview] = useState<Json | null>(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [interval, setInterval] = useState(15);
  const refresh = async () => {
    try {
      const value = await api<Json>(
        '/api/memory/status' + query({ project: project.id, worktree }),
      );
      setStatus(value);
      setInterval(Number(object(value.auto).interval_minutes ?? 15));
    } catch (err) {
      setError(message(err));
    }
  };
  useEffect(() => {
    setPreview(null);
    void refresh();
  }, [project.id, worktree]);
  const act = async (kind: 'preview' | 'save' | 'auto') => {
    setBusy(true);
    setError('');
    try {
      if (kind === 'auto') {
        await api('/api/memory/settings', {
          method: 'PATCH',
          body: JSON.stringify({
            project: project.id,
            worktree,
            enabled: !object(status.auto).enabled,
            interval_minutes: interval,
          }),
        });
      } else {
        const value = await post<Json>(
          '/api/memory/' + (kind === 'save' ? 'snapshot' : 'preview'),
          { project: project.id, worktree },
        );
        setPreview(value);
      }
      await refresh();
      onChange();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  const auto = object(status.auto),
    latest = object(status.latest);
  return (
    <div className="memory-settings">
      <div className="section-title">
        <GitBranch size={16} />
        <h3>Pamięć razem z projektem</h3>
      </div>
      <p className="hint">
        Pełny snapshot obejmuje rozmowy i źródła. Sprawdź zakres przed dodaniem go do repozytorium;
        treść może zawierać prywatne dane.
      </p>
      <dl className="metadata">
        <div>
          <dt>Ostatni snapshot</dt>
          <dd>{formatTime(latest.created_at)}</dd>
        </div>
        <div>
          <dt>Automat</dt>
          <dd>{auto.enabled ? 'Włączony' : 'Wyłączony'}</dd>
        </div>
        <div>
          <dt>Ostatnia udana kontrola</dt>
          <dd>{formatTime(auto.last_success_at)}</dd>
        </div>
        <div>
          <dt>Miejsce zapisu</dt>
          <dd className="path">{label(status.path) || '.memory/devbrain/'}</dd>
        </div>
      </dl>
      <div className="form-actions">
        <button className="button" disabled={busy} onClick={() => void act('preview')}>
          Podgląd zapisu
        </button>
        <button
          className="button primary"
          disabled={busy || !preview}
          onClick={() => void act('save')}
        >
          Zapisz snapshot
        </button>
      </div>
      {preview && (
        <SmallData
          data={{
            status: preview.status,
            counts: preview.counts,
            bytes: preview.bytes,
            processing: preview.processing,
          }}
        />
      )}
      <div className="auto-setting">
        <label>
          Interwał (min)
          <input
            type="number"
            min="1"
            max="10080"
            value={interval}
            onChange={(e) => setInterval(Number(e.target.value))}
          />
        </label>
        <button className="button" disabled={busy || interval < 1} onClick={() => void act('auto')}>
          {auto.enabled ? 'Wyłącz automat' : 'Włącz automat'}
        </button>
      </div>
      {Boolean(error || auto.error || status.error) && (
        <p className="form-error" role="alert">
          {error || label(auto.error) || label(status.error)}
        </p>
      )}
      {Boolean(status.processing) && <SmallData data={status.processing} />}
    </div>
  );
}
export function Inspector({
  project,
  worktree,
  refresh,
  selectedNode,
  onChange,
}: {
  project: Project;
  worktree: string;
  refresh: number;
  selectedNode?: GraphNode;
  onChange: () => void;
}) {
  const [tab, setTab] = useState('records'),
    [records, setRecords] = useState<RecordItem[]>([]),
    [sources, setSources] = useState<SourceItem[]>([]),
    [plans, setPlans] = useState<Json[]>([]),
    [checks, setChecks] = useState<Json[]>([]),
    [jobs, setJobs] = useState<Json[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [kind, setKind] = useState(''),
    [search, setSearch] = useState(''),
    [error, setError] = useState(''),
    [loading, setLoading] = useState(false);
  const [record, setRecord] = useState<RecordItem | null>(null),
    [recordExtra, setRecordExtra] = useState<Json>({}),
    [history, setHistory] = useState<Json[] | null>(null),
    [historyCursor, setHistoryCursor] = useState<string | null>(null),
    [source, setSource] = useState<SourceItem | null>(null),
    [sourceText, setSourceText] = useState(''),
    [sourceOffset, setSourceOffset] = useState<number | null>(null),
    [view, setView] = useState('normalized'),
    [detail, setDetail] = useState<Json | null>(null),
    [reason, setReason] = useState('');
  const scopeRef = useRef('');
  scopeRef.current = project.id + '\0' + worktree;
  const clear = () => {
    setRecord(null);
    setSource(null);
    setDetail(null);
    setHistory(null);
    setError('');
  };
  useEffect(() => {
    clear();
    setCursor(null);
  }, [project.id, worktree, tab]);
  const fetchRecords = async (next?: string) => {
    const requestedScope = scopeRef.current;
    const page = await api<Page<RecordItem>>(
      '/api/inspector/records' +
        query({ project: project.id, worktree, kind, cursor: next, limit: 100 }),
    );
    if (scopeRef.current !== requestedScope) return;
    setRecords((old) => (next ? [...old, ...page.items] : page.items));
    setCursor(page.next_cursor ?? null);
  };
  const fetchSources = async (next?: string) => {
    const requestedScope = scopeRef.current;
    const page = await api<Page<SourceItem>>(
      '/api/inspector/sources' + query({ project: project.id, cursor: next, limit: 100 }),
    );
    if (scopeRef.current !== requestedScope) return;
    setSources((old) => (next ? [...old, ...page.items] : page.items));
    setCursor(page.next_cursor ?? null);
  };
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    const run = async () => {
      try {
        if (tab === 'records') await fetchRecords();
        else if (tab === 'sources') await fetchSources();
        else if (tab === 'plans') {
          const value = await api<Json>('/api/inspector/plans' + query({ project: project.id }));
          if (alive) setPlans(entries(value.plans ?? value.items));
          if (alive) setCursor(label(value.next_cursor) || null);
        } else if (tab === 'checks') {
          const value = await api<Json>(
            '/api/inspector/checks' + query({ project: project.id, worktree }),
          );
          if (alive) setChecks(entries(value.runs ?? value.checks ?? value.items));
          if (alive) setCursor(label(value.next_cursor) || null);
        } else if (tab === 'jobs') {
          const value = await api<Page<Json>>(
            '/api/inspector/jobs' + query({ project: project.id, limit: 100 }),
          );
          if (alive) {
            setJobs(value.items);
            setCursor(value.next_cursor ?? null);
          }
        }
      } catch (err) {
        if (alive) setError(message(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void run();
    return () => {
      alive = false;
    };
  }, [project.id, worktree, tab, kind, refresh]);
  const openRecord = async (id: string) => {
    const requestedScope = scopeRef.current;
    try {
      setError('');
      const value = await api<Json>(
        '/api/inspector/records/' + encodeURIComponent(id) + query({ project: project.id }),
      );
      if (scopeRef.current !== requestedScope) return;
      setRecord((value.record ?? value) as RecordItem);
      setRecordExtra(value);
      setSource(null);
      setDetail(null);
      setHistory(null);
      const managed = label(object(object(value.record ?? value).metadata).plan_ref);
      if (managed) {
        const plan = await api<Json>(
          '/api/inspector/plans/' + encodeURIComponent(managed) + query({ project: project.id }),
        );
        if (scopeRef.current === requestedScope)
          setRecordExtra({ ...value, expected_plan_version: object(plan.item ?? plan).version });
      }
    } catch (err) {
      setError(message(err));
    }
  };
  const openSource = async (id: string, mode = view, offset = 0) => {
    const requestedScope = scopeRef.current;
    try {
      setError('');
      const value = await api<Json>(
        '/api/inspector/sources/' +
          encodeURIComponent(id) +
          query({ project: project.id, view: mode, offset, limit: 65536 }),
      );
      if (scopeRef.current !== requestedScope) return;
      setSource((value.source ?? { id }) as SourceItem);
      setSourceText((old) => (offset ? old + label(value.text) : label(value.text)));
      setSourceOffset(typeof value.next_offset === 'number' ? value.next_offset : null);
      setView(mode);
      setRecord(null);
      setDetail(null);
    } catch (err) {
      setError(message(err));
    }
  };
  useEffect(() => {
    if (!selectedNode) return;
    const node = selectedNode,
      metadata = object(node.metadata),
      recordId =
        label(node.record_id) ||
        (node.owner_type === 'record' ? label(node.owner_id) : '') ||
        label(metadata.record_id) ||
        (node.id.startsWith('record:') ? node.id.slice(7) : ''),
      planId =
        label(node.plan_id) ||
        (node.owner_type === 'plan' ? label(node.owner_id) : '') ||
        label(metadata.plan_id) ||
        (node.id.startsWith('plan:') ? node.id.slice(5) : '');
    if (node.entity_type === 'revision' && (recordId || planId)) {
      setTab(recordId ? 'records' : 'plans');
      setRecord(null);
      setSource(null);
      setDetail({ title: 'Wczytywanie zapisanej wersji…' });
      const requestedScope = scopeRef.current;
      void (async () => {
        let cursor: string | undefined;
        do {
          const page = await api<Page<Json>>(
            '/api/inspector/' +
              (recordId ? 'records/' : 'plans/') +
              encodeURIComponent(recordId || planId) +
              '/revisions' +
              query({ project: project.id, cursor, limit: 100 }),
          );
          if (scopeRef.current !== requestedScope) return;
          const revision = page.items.find(
            (item) => item.id === node.revision_id || item.version === node.version,
          );
          if (revision) {
            setDetail({ title: 'Wersja historyczna · v' + label(revision.version), ...revision });
            return;
          }
          cursor = page.next_cursor ?? undefined;
        } while (cursor);
        setError('Nie znaleziono wskazanej wersji. Odśwież graf.');
      })().catch((err) => setError(message(err)));
    } else if (recordId) {
      setTab('records');
      void openRecord(recordId);
    } else if (planId) {
      setTab('plans');
      setRecord(null);
      setSource(null);
      void api<Json>(
        '/api/inspector/plans/' + encodeURIComponent(planId) + query({ project: project.id }),
      )
        .then((value) => setDetail(object(value.item ?? value)))
        .catch((err) => setError(message(err)));
    } else if (node.source_id || node.id.startsWith('source:')) {
      setTab('sources');
      void openSource(node.source_id ?? node.id.slice(7));
    } else setDetail(node);
  }, [selectedNode]);
  const review = async (action: string) => {
    if (!record) return;
    try {
      setError('');
      const planVersion = recordExtra.expected_plan_version ?? object(recordExtra.plan).version;
      await post('/api/inspector/records/' + encodeURIComponent(record.id) + '/review', {
        project: project.id,
        action,
        expected_version: record.version,
        ...(planVersion ? { expected_plan_version: planVersion } : {}),
        ...(reason ? { reason } : {}),
      });
      setReason('');
      await openRecord(record.id);
      onChange();
    } catch (err) {
      setError(message(err));
      await openRecord(record.id);
      setError(message(err));
    }
  };
  const showHistory = async (next?: string) => {
    if (!record) return;
    try {
      const value = await api<Page<Json>>(
        '/api/inspector/records/' +
          encodeURIComponent(record.id) +
          '/revisions' +
          query({ project: project.id, cursor: next, limit: 100 }),
      );
      setHistory((old) => (next ? [...(old ?? []), ...value.items] : value.items));
      setHistoryCursor(value.next_cursor ?? null);
    } catch (err) {
      setError(message(err));
    }
  };
  const loadProjection = async (next: string) => {
    const requestedScope = scopeRef.current;
    const value = await api<Page<Json>>(
      '/api/inspector/' +
        tab +
        query({
          project: project.id,
          worktree: tab === 'checks' ? worktree : undefined,
          cursor: next,
          limit: 100,
        }),
    );
    if (scopeRef.current !== requestedScope) return;
    if (tab === 'plans') setPlans((old) => [...old, ...value.items]);
    if (tab === 'checks') setChecks((old) => [...old, ...value.items]);
    if (tab === 'jobs') setJobs((old) => [...old, ...value.items]);
    setCursor(value.next_cursor ?? null);
  };
  const filteredRecords = records.filter(
    (item) =>
      !search || `${item.content} ${item.kind}`.toLowerCase().includes(search.toLowerCase()),
  );
  const filteredSources = sources.filter(
    (item) =>
      !search ||
      `${item.uri ?? ''} ${item.kind ?? ''}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <section className="inspector panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">SHARED KNOWLEDGE</span>
          <h2>Pamięć projektu</h2>
        </div>
        <div className="heading-actions">
          <Badge tone="good">ArcadeDB</Badge>
          <IconButton label="Odśwież pamięć" onClick={onChange}>
            <RefreshCw size={14} />
          </IconButton>
        </div>
      </div>
      <div className="inspector-tabs" role="tablist" aria-label="Widoki pamięci">
        {[
          ['records', 'Ustalenia'],
          ['sources', 'Źródła'],
          ['plans', 'Plany i zadania'],
          ['checks', 'Testy'],
          ['jobs', 'Kolejka'],
          ['memory', 'Snapshot'],
        ].map(([id, title]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => {
              setTab(id);
              clear();
            }}
          >
            {title}
          </button>
        ))}
      </div>
      <div className="inspector-body">
        {error && (
          <div className="inline-error" role="status">
            {error}
          </div>
        )}
        {tab === 'memory' ? (
          <MemoryPanel
            key={project.id + ':' + worktree}
            project={project}
            worktree={worktree}
            onChange={onChange}
          />
        ) : record ? (
          <div className="record-detail">
            <button className="back-button" onClick={clear}>
              <ArrowLeft size={13} />
              Lista ustaleń
            </button>
            <div className="record-title">
              <span>{names[record.kind] ?? record.kind}</span>
              <Badge tone={tone(record.status)}>{names[record.status] ?? record.status}</Badge>
              <span className="version">v{record.version}</span>
            </div>
            <p className="record-content">{record.content}</p>
            {record.kind === 'task' && (
              <Badge>{names[label(record.metadata?.state)] ?? label(record.metadata?.state)}</Badge>
            )}
            {Boolean(object(recordExtra.verification).status) && (
              <p className="hint">
                Weryfikacja:{' '}
                <Badge tone={tone(label(object(recordExtra.verification).status))}>
                  {label(object(recordExtra.verification).status)}
                </Badge>
              </p>
            )}
            <div className="source-links">
              <span>Dowody</span>
              {(record.source_ids ?? []).map((id) => (
                <button key={id} onClick={() => void openSource(id)}>
                  <FileText size={13} />
                  {id.slice(0, 12)}
                  <ArrowUpRight size={12} />
                </button>
              ))}
            </div>
            <div className="record-review">
              <input
                aria-label="Uzasadnienie oceny ustalenia"
                placeholder="Uzasadnienie (wymagane przy kwestionowaniu)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <div className="form-actions">
                <button
                  className="button"
                  onClick={() => void review('confirm')}
                  disabled={
                    record.status === 'superseded' ||
                    Boolean(record.metadata?.plan_ref && !recordExtra.expected_plan_version)
                  }
                >
                  <Check size={13} />
                  Potwierdź
                </button>
                <button
                  className="button"
                  onClick={() => void review('dispute')}
                  disabled={
                    record.status === 'superseded' ||
                    !reason.trim() ||
                    Boolean(record.metadata?.plan_ref && !recordExtra.expected_plan_version)
                  }
                >
                  <X size={13} />
                  Zakwestionuj
                </button>
                <button className="button subtle" onClick={() => void showHistory()}>
                  <History size={13} />
                  Historia
                </button>
              </div>
            </div>
            {history && (
              <div className="revision-list">
                {history.map((item) => (
                  <div key={label(item.id) || label(item.version)}>
                    <span>
                      v{label(item.version)} · {formatTime(item.recorded_at)}
                    </span>
                    <p>{label(object(item.snapshot).content)}</p>
                    <Badge>{label(object(item.snapshot).status)}</Badge>
                  </div>
                ))}
                {historyCursor && (
                  <button className="load-more" onClick={() => void showHistory(historyCursor)}>
                    Starsze wersje
                  </button>
                )}
              </div>
            )}
            <details>
              <summary>Identyfikatory i metadane</summary>
              <SmallData data={{ id: record.id, metadata: record.metadata }} />
            </details>
          </div>
        ) : source ? (
          <div className="source-detail">
            <button className="back-button" onClick={clear}>
              <ArrowLeft size={13} />
              Lista źródeł
            </button>
            <div className="record-title">
              <FileText size={15} />
              <strong>{source.kind ?? 'Źródło'}</strong>
            </div>
            <p className="path">{source.uri ?? source.id}</p>
            <div className="segmented">
              <button
                className={view === 'normalized' ? 'active' : ''}
                onClick={() => void openSource(source.id, 'normalized')}
              >
                Tekst
              </button>
              <button
                className={view === 'raw' ? 'active' : ''}
                onClick={() => void openSource(source.id, 'raw')}
              >
                Surowy zapis
              </button>
              <button
                className={view === 'metadata' ? 'active' : ''}
                onClick={() => void openSource(source.id, 'metadata')}
              >
                Metadane
              </button>
            </div>
            <pre className="source-text">{sourceText || '(Puste źródło)'}</pre>
            {sourceOffset !== null && (
              <button
                className="button"
                onClick={() => void openSource(source.id, view, sourceOffset)}
              >
                Wczytaj dalszy fragment
              </button>
            )}
            <small className="muted">SHA-256: {source.sha256 ?? 'brak'}</small>
          </div>
        ) : detail ? (
          <div className="record-detail">
            <button className="back-button" onClick={clear}>
              <ArrowLeft size={13} />
              Wróć
            </button>
            <h3>
              {label(detail.title) ||
                label(detail.profile_id) ||
                label(detail.label) ||
                'Szczegóły'}
            </h3>
            <SmallData data={detail} />
          </div>
        ) : (
          <>
            {(tab === 'records' || tab === 'sources') && (
              <div className="inspector-filters">
                <div className="search-field">
                  <Search size={13} />
                  <input
                    value={search}
                    aria-label="Filtruj listę pamięci"
                    placeholder="Szukaj w załadowanych wpisach…"
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>
                {tab === 'records' && (
                  <select
                    aria-label="Typ ustalenia"
                    value={kind}
                    onChange={(e) => setKind(e.target.value)}
                  >
                    <option value="">Wszystkie typy</option>
                    {[
                      'decision',
                      'task',
                      'fact',
                      'constraint',
                      'checkpoint',
                      'question',
                      'preference',
                    ].map((id) => (
                      <option key={id} value={id}>
                        {names[id]}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            )}
            {loading ? (
              <div className="loading-row">
                <Busy />
              </div>
            ) : tab === 'records' ? (
              <div className="record-list">
                {filteredRecords.map((item) => (
                  <button
                    className="record-row"
                    key={item.id}
                    onClick={() => void openRecord(item.id)}
                  >
                    <span className={'record-kind ' + item.kind}>
                      {item.kind === 'task' ? (
                        <ShieldCheck size={15} />
                      ) : item.kind === 'decision' ? (
                        <GitBranch size={15} />
                      ) : (
                        <FileText size={15} />
                      )}
                    </span>
                    <span className="record-row-content">
                      <span>{item.content.split('\n')[0]}</span>
                      <small>
                        {names[item.kind] ?? item.kind} · v{item.version}
                      </small>
                    </span>
                    <Badge tone={tone(item.status)}>
                      {item.kind === 'task'
                        ? (names[label(item.metadata?.state)] ?? label(item.metadata?.state))
                        : (names[item.status] ?? item.status)}
                    </Badge>
                    <ChevronRight size={13} />
                  </button>
                ))}
                {!filteredRecords.length && (
                  <Empty title="Brak zapisanych ustaleń">
                    Nowe propozycje pojawią się tutaj wraz ze źródłami.
                  </Empty>
                )}
              </div>
            ) : tab === 'sources' ? (
              <div className="record-list">
                {filteredSources.map((item) => (
                  <button
                    className="record-row"
                    key={item.id}
                    onClick={() => void openSource(item.id)}
                  >
                    <span className="record-kind source">
                      <FileText size={15} />
                    </span>
                    <span className="record-row-content">
                      <span>{item.uri ?? item.id}</span>
                      <small>{item.kind ?? 'źródło'}</small>
                    </span>
                    <ChevronRight size={13} />
                  </button>
                ))}
                {!filteredSources.length && <Empty title="Brak źródeł" />}
              </div>
            ) : tab === 'plans' ? (
              <div className="record-list">
                {plans.map((item) => (
                  <button
                    className="record-row"
                    key={label(item.id)}
                    onClick={() =>
                      void api<Json>(
                        '/api/inspector/plans/' +
                          encodeURIComponent(label(item.id)) +
                          query({ project: project.id }),
                      )
                        .then((value) => setDetail(object(value.item ?? value)))
                        .catch((err) => setError(message(err)))
                    }
                  >
                    <span className="record-kind plan">
                      <GitBranch size={15} />
                    </span>
                    <span className="record-row-content">
                      <span>{label(item.title)}</span>
                      <small>
                        v{label(item.version)} ·{' '}
                        {Array.isArray(item.step_ids) ? item.step_ids.length : 0} kroków
                      </small>
                    </span>
                    <ChevronRight size={13} />
                  </button>
                ))}
                {!plans.length && (
                  <Empty title="Brak planów">
                    Plan zapisany przez klienta będzie dostępny dla wszystkich agentów.
                  </Empty>
                )}
              </div>
            ) : tab === 'jobs' ? (
              <div className="record-list">
                {jobs.map((item) => (
                  <button
                    className="record-row"
                    key={label(item.id)}
                    onClick={() =>
                      void api<Json>(
                        '/api/inspector/jobs/' +
                          encodeURIComponent(label(item.id)) +
                          query({ project: project.id }),
                      )
                        .then((value) => setDetail(object(value.item ?? value)))
                        .catch((err) => setError(message(err)))
                    }
                  >
                    <span className="record-kind">
                      <History size={15} />
                    </span>
                    <span className="record-row-content">
                      <span>{label(item.kind)}</span>
                      <small>
                        Próba {label(item.attempts)} ·{' '}
                        {label(item.error_code) || formatTime(item.updated_at)}
                      </small>
                    </span>
                    <Badge
                      tone={
                        item.state === 'failed'
                          ? 'warn'
                          : item.state === 'completed'
                            ? 'good'
                            : 'muted'
                      }
                    >
                      {label(item.state)}
                    </Badge>
                  </button>
                ))}
                {!jobs.length && <Empty title="Kolejka jest pusta" />}
              </div>
            ) : (
              <div className="record-list">
                {checks.map((item) => (
                  <button
                    className="record-row"
                    key={label(item.id)}
                    onClick={() => setDetail(item)}
                  >
                    <span className="record-kind">
                      <ShieldCheck size={15} />
                    </span>
                    <span className="record-row-content">
                      <span>{label(item.profile_id) || 'Sprawdzenie'}</span>
                      <small>
                        {formatTime(item.ended_at)} · exit {label(item.returncode)}
                      </small>
                    </span>
                    <Badge tone={item.passed ? 'good' : 'warn'}>
                      {item.passed ? 'passed' : label(item.outcome)}
                    </Badge>
                  </button>
                ))}
                {!checks.length && (
                  <Empty title="Brak wyników testów">
                    Testy uruchamiasz jawnie z CLI. Kokpit pokazuje zapisane dowody.
                  </Empty>
                )}
              </div>
            )}
            {cursor && ['records', 'sources', 'plans', 'checks', 'jobs'].includes(tab) && (
              <button
                className="load-more"
                onClick={() =>
                  void (
                    tab === 'records'
                      ? fetchRecords(cursor)
                      : tab === 'sources'
                        ? fetchSources(cursor)
                        : loadProjection(cursor)
                  ).catch((err) => setError(message(err)))
                }
              >
                Wczytaj więcej
              </button>
            )}
          </>
        )}
      </div>
      <div className="inspector-footer">
        <span className="status-dot good" />
        Wspólna pamięć wszystkich agentów<span>Źródła i wersje są zachowane</span>
      </div>
    </section>
  );
}
