import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownToLine,
  ArrowUpRight,
  BrainCircuit,
  Check,
  ChevronDown,
  CircleAlert,
  Folder,
  FolderPlus,
  FolderSearch,
  Layers3,
  LogIn,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  SquareTerminal,
  X,
} from 'lucide-react';
import { api, bootstrap, message, post, put, query } from './api';
import {
  Badge,
  Busy,
  Empty,
  IconButton,
  label,
  Modal,
  object,
  entries,
  SmallData,
} from './components';
import { BrainGraph } from './BrainGraph';
import { InboxBrowser } from './InboxBrowser';
import { Inspector, MemoryImportForm } from './Inspector';
import { TerminalPanel } from './TerminalPanel';
import type { ClientRun, CockpitState, GraphNode, Json, Profile, Settings, Slot } from './types';
import { defaultSettings } from './types';

type Dialog =
  | 'project'
  | 'discovery'
  | 'settings'
  | 'sessions'
  | 'import'
  | 'auth'
  | 'inbox'
  | null;
function statusLabel(value: Json): { title: string; tone: string } {
  const auth = object(value.auth),
    installation = object(value.installation),
    usage = object(value.usage);
  const install =
      label(value.installation_status) || label(value.installation) || label(installation.status),
    state =
      label(value.auth_status) || label(value.auth) || label(auth.status) || label(value.status);
  if (value.installed === false || install === 'missing')
    return { title: 'Brak programu', tone: 'warn' };
  if (
    (label(value.usage) || label(usage.status)) === 'quota_exhausted' ||
    state === 'quota_exhausted'
  )
    return { title: 'Wyczerpany limit', tone: 'warn' };
  if (['signed_out', 'not_logged_in', 'missing', 'expired', 'expired_or_rejected'].includes(state))
    return { title: state.includes('expired') ? 'Logowanie wygasło' : 'Zaloguj się', tone: 'warn' };
  if (
    value.loggedIn === true ||
    ['reported_signed_in', 'signed_in', 'authenticated', 'ready'].includes(state)
  )
    return { title: 'Zalogowany', tone: 'good' };
  return { title: 'Sprawdź połączenie', tone: 'muted' };
}
export default function App() {
  const [launchRequested, setLaunchRequested] = useState(false),
    [ready, setReady] = useState(false),
    [fatal, setFatal] = useState(''),
    [state, setState] = useState<CockpitState>({
      projects: [],
      profiles: [],
      settings: defaultSettings,
      runs: [],
    }),
    [selected, setSelected] = useState(''),
    [worktree, setWorktree] = useState(''),
    [filter, setFilter] = useState(''),
    [dialog, setDialog] = useState<Dialog>(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [refresh, setRefresh] = useState(0),
    [live, setLive] = useState(false),
    [selectedNode, setSelectedNode] = useState<GraphNode>(),
    [auth, setAuth] = useState<Record<string, Json>>({}),
    [inboxId, setInboxId] = useState<string>(),
    [authClient, setAuthClient] = useState('codex'),
    [authRun, setAuthRun] = useState<ClientRun>(),
    [discovery, setDiscovery] = useState<Json>({}),
    [projectName, setProjectName] = useState(''),
    [projectRoot, setProjectRoot] = useState(''),
    [detectedProject, setDetectedProject] = useState<{
      name: string;
      root: string;
      path: string;
    } | null>(null),
    [importPath, setImportPath] = useState(''),
    [importRoot, setImportRoot] = useState(''),
    [scanRoots, setScanRoots] = useState(''),
    [draftProfiles, setDraftProfiles] = useState<Profile[]>([]),
    [draftSlots, setDraftSlots] = useState<Slot[]>([]),
    [presetScope, setPresetScope] = useState('project');
  const pendingLaunch = useRef<{ scope: string; key: string } | null>(null),
    launchKey = useRef(''),
    eventTimer = useRef<ReturnType<typeof setTimeout> | null>(null),
    mounted = useRef(true);
  const project = state.projects.find((item) => item.id === selected);
  const slots =
    selected && state.settings.project_slots[selected]
      ? state.settings.project_slots[selected]
      : state.settings.default_slots;
  const refreshState = useCallback(async () => {
    try {
      const value = await api<CockpitState>('/api/state');
      if (!mounted.current) return;
      setState({
        ...value,
        settings: { ...defaultSettings, ...value.settings },
        projects: value.projects ?? [],
        profiles: value.profiles ?? [],
        runs: value.runs ?? [],
      });
    } catch (err) {
      if (mounted.current) setError(message(err));
    }
  }, []);
  const refreshAuth = useCallback(async () => {
    await Promise.all(
      ['codex', 'claude'].map(async (client) => {
        try {
          const value = await api<Json>('/api/auth' + query({ client }));
          if (mounted.current) setAuth((old) => ({ ...old, [client]: value }));
        } catch (err) {
          if (mounted.current)
            setAuth((old) => ({ ...old, [client]: { status: 'unknown', message: message(err) } }));
        }
      }),
    );
  }, []);
  useEffect(() => {
    mounted.current = true;
    let active = true;
    void bootstrap()
      .then(async () => {
        await refreshState();
        if (active) {
          setReady(true);
          void refreshAuth();
        }
      })
      .catch((err) => {
        if (active) setFatal(message(err));
      });
    return () => {
      active = false;
      mounted.current = false;
    };
  }, [refreshState, refreshAuth]);
  useEffect(() => {
    if (!ready) return;
    const timer = setInterval(() => void refreshState(), 5000);
    return () => clearInterval(timer);
  }, [ready, refreshState]);
  useEffect(() => {
    if (!ready) return;
    const events = new EventSource('/api/events', { withCredentials: true });
    const change = () => {
      if (eventTimer.current) clearTimeout(eventTimer.current);
      eventTimer.current = setTimeout(() => {
        setRefresh((value) => value + 1);
        void refreshState();
      }, 250);
    };
    events.onopen = () => setLive(true);
    events.onerror = () => setLive(false);
    events.addEventListener('change', change);
    events.addEventListener('reset', change);
    return () => {
      events.close();
      if (eventTimer.current) clearTimeout(eventTimer.current);
    };
  }, [ready, refreshState]);
  useEffect(() => {
    if (!ready) return;
    if (selected && !state.projects.some((item) => item.id === selected)) {
      setSelected('');
      return;
    }
    if (!selected && state.projects.length) {
      const preferred =
        state.projects.find((item) => item.id === state.settings.last_project) ?? state.projects[0];
      setSelected(preferred.id);
    }
  }, [ready, state.projects, selected, state.settings.last_project]);
  useEffect(() => {
    if (!project) {
      setWorktree('');
      return;
    }
    if (!(project.roots ?? [project.root]).includes(worktree)) setWorktree(project.root);
    setSelectedNode(undefined);
  }, [selected, project?.root]);
  const change = () => {
    setRefresh((value) => value + 1);
    void refreshState();
  };
  const selectProject = (id: string) => {
    const target = state.projects.find((item) => item.id === id);
    setSelected(id);
    setWorktree(id === selected ? worktree : (target?.root ?? ''));
    setLaunchRequested(true);
    setError('');
    void put('/api/settings', { last_project: id }).catch((err) => setError(message(err)));
  };
  const launch = useCallback(
    async (force = false) => {
      if (!project || !worktree || !slots.length) return;
      const key = JSON.stringify([project.id, worktree, slots]);
      if (!force && launchKey.current === key) return;
      launchKey.current = key;
      const requestKey =
        pendingLaunch.current?.scope === key ? pendingLaunch.current.key : crypto.randomUUID();
      pendingLaunch.current = { scope: key, key: requestKey };
      try {
        const result = await post<Json>('/api/client-runs', {
          project: project.id,
          worktree,
          slots,
          idempotency_key: requestKey,
        });
        pendingLaunch.current = null;
        const problems = entries(result.errors);
        if (problems.length)
          setError(
            problems
              .map((item) => label(item.message) || label(item.error) || JSON.stringify(item))
              .join(' · '),
          );
        await refreshState();
      } catch (err) {
        setError(message(err));
      }
    },
    [project?.id, worktree, JSON.stringify(slots), refreshState],
  );
  useEffect(() => {
    if (ready && project && worktree && launchRequested) {
      setLaunchRequested(false);
      void launch(true);
    }
  }, [ready, launch, launchRequested]);
  const launchSlot = async (slot: Slot, resume: boolean) => {
    if (!project || !worktree) return;
    setError('');
    try {
      const result = await post<Json>('/api/client-runs', {
        project: project.id,
        worktree,
        slots: [slot],
        resume,
        idempotency_key: crypto.randomUUID(),
      });
      const problems = entries(result.errors);
      if (problems.length)
        setError(
          problems
            .map(
              (item) =>
                [label(item.code), label(item.message) || label(item.error)]
                  .filter(Boolean)
                  .join(': ') || JSON.stringify(item),
            )
            .join(' · '),
        );
      await refreshState();
    } catch (err) {
      setError(message(err));
    }
  };
  const saveSlots = async (next: Slot[]) => {
    if (!project) return;
    try {
      await put('/api/settings', {
        project_slots: { ...state.settings.project_slots, [project.id]: next },
      });
      await refreshState();
    } catch (err) {
      setError(message(err));
    }
  };
  const openDialog = (kind: Dialog) => {
    setError('');
    setDialog(kind);
    if (kind === 'project') setDetectedProject(null);
    if (kind === 'settings') {
      setDraftProfiles(state.profiles.map((item) => ({ ...item })));
      setDraftSlots(slots.map((item) => ({ ...item })));
      setPresetScope(project ? 'project' : 'global');
    }
    if (kind === 'discovery') {
      setScanRoots(state.settings.scan_roots.join('\n'));
      void api<Json>('/api/discovery')
        .then(setDiscovery)
        .catch((err) => setError(message(err)));
    }
  };
  const openImport = (path = '', root = project?.root ?? '') => {
    setImportPath(path);
    setImportRoot(root);
    setError('');
    setDialog('import');
  };
  useEffect(() => {
    if (dialog !== 'discovery') return;
    const timer = setInterval(
      () =>
        void api<Json>('/api/discovery')
          .then(setDiscovery)
          .catch((err) => setError(message(err))),
      1500,
    );
    return () => clearInterval(timer);
  }, [dialog]);
  const register = async (name: string, root: string, ignoreSnapshot = false) => {
    setBusy(true);
    setError('');
    try {
      if (!ignoreSnapshot) {
        const inspected = await post<Json>('/api/projects/inspect', { root });
        if (inspected.memory_snapshot) {
          setProjectName(name);
          setProjectRoot(label(inspected.root) || root);
          setDetectedProject({
            name,
            root: label(inspected.root) || root,
            path: label(inspected.memory_snapshot) || root + '/.memory/devbrain',
          });
          setDialog('project');
          return;
        }
      }
      const result = await post<Json>('/api/projects', { name, root });
      const created = object(result.project ?? result);
      await refreshState();
      setSelected(label(created.id));
      setWorktree(root);
      setLaunchRequested(true);
      setDialog(null);
      setProjectName('');
      setProjectRoot('');
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  const startAuth = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await post<Json>('/api/auth/login', { client: authClient });
      setAuthRun((result.run ?? result) as ClientRun);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  const showAuth = (client: string) => {
    setAuthClient(client);
    setAuthRun(undefined);
    setDialog('auth');
    setError('');
    void refreshAuth();
  };
  const activeRuns = state.runs.filter(
    (run) => !['stopped', 'exited', 'failed', 'lost'].includes(run.lifecycle ?? run.state ?? ''),
  );
  const clientRun = (slot: Slot) =>
    state.runs
      .filter(
        (run) =>
          run.project_id === project?.id &&
          run.worktree === worktree &&
          run.slot_id === slot.id &&
          run.client === state.profiles.find((profile) => profile.id === slot.profile_id)?.client,
      )
      .sort((a, b) => (b.generation ?? 0) - (a.generation ?? 0))[0];
  const settingsSave = async () => {
    setBusy(true);
    setError('');
    try {
      await put('/api/profiles', { profiles: draftProfiles });
      const patch: Partial<Settings> =
        presetScope === 'global'
          ? { default_slots: draftSlots }
          : { project_slots: { ...state.settings.project_slots, [selected]: draftSlots } };
      await put('/api/settings', patch);
      await refreshState();
      setDialog(null);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  if (fatal)
    return (
      <div className="startup">
        <BrainCircuit size={44} />
        <h1>DevBrain</h1>
        <p role="alert">{fatal}</p>
        <p>
          Otwórz lokalny kokpit ponownie poleceniem <code>devbrain</code>.
        </p>
      </div>
    );
  if (!ready)
    return (
      <div className="startup">
        <BrainCircuit size={44} />
        <h1>DevBrain</h1>
        <Busy>Otwieranie lokalnego środowiska…</Busy>
      </div>
    );
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="/"
          onClick={(event) => event.preventDefault()}
          aria-label="DevBrain — lokalne środowisko"
        >
          <span className="brand-mark">
            <BrainCircuit size={25} />
          </span>
          <span>
            DevBrain<small>YOUR LOCAL WORKSPACE</small>
          </span>
        </a>
        <div className="sidebar-section-heading">
          <span>PROJEKTY</span>
          <div>
            <IconButton
              label="Znajdź projekty w wybranych katalogach"
              onClick={() => openDialog('discovery')}
            >
              <FolderSearch size={15} />
            </IconButton>
            <IconButton label="Dodaj projekt" onClick={() => openDialog('project')}>
              <Plus size={16} />
            </IconButton>
          </div>
        </div>
        <div className="sidebar-search">
          <Search size={13} />
          <input
            aria-label="Filtruj projekty"
            placeholder="Znajdź projekt…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <kbd>⌕</kbd>
        </div>
        <nav className="project-list" aria-label="Projekty na tym komputerze">
          {state.projects
            .filter((item) =>
              `${item.name} ${item.root}`.toLowerCase().includes(filter.toLowerCase()),
            )
            .map((item) => (
              <button
                key={item.id}
                className={'project-item ' + (selected === item.id ? 'active' : '')}
                onClick={() => selectProject(item.id)}
              >
                <span className="project-icon">
                  <Folder size={17} />
                </span>
                <span>
                  <strong>{item.name}</strong>
                  <small>{item.root.split('/').filter(Boolean).slice(-2).join('/')}</small>
                </span>
                <span
                  className={
                    'project-indicator ' +
                    (state.runs.some(
                      (run) =>
                        run.project_id === item.id &&
                        !['exited', 'stopped', 'lost', 'failed'].includes(
                          run.lifecycle ?? run.state ?? '',
                        ),
                    )
                      ? 'running'
                      : '')
                  }
                />
              </button>
            ))}
          {!state.projects.length && (
            <p className="sidebar-hint">Dodaj projekt lub wyszukaj go w wybranych katalogach.</p>
          )}
        </nav>
        <button className="sidebar-action" onClick={() => openDialog('discovery')}>
          <FolderSearch size={16} />
          Znajdź projekty
          <ArrowUpRight size={13} />
        </button>
        <button className="sidebar-action" onClick={() => openImport()}>
          <ArrowDownToLine size={16} />
          Wczytaj pamięć
        </button>
        <button
          className="sidebar-action"
          onClick={() => {
            setInboxId(undefined);
            openDialog('inbox');
          }}
        >
          <CircleAlert size={16} />
          Nieprzypisane wpisy
        </button>
        <div className="sidebar-bottom">
          <div className="local-badge">
            <span className="status-dot good" />
            Lokalnie na Twoim komputerze
          </div>
          <button className="sidebar-settings" onClick={() => openDialog('settings')}>
            <Settings2 size={16} />
            <span>Ustawienia środowiska</span>
          </button>
          <div className="sidebar-version">
            <span>DevBrain 0.5</span>
            <span>CLI + shared memory</span>
          </div>
        </div>
      </aside>
      <main className="workspace">
        <header className="workspace-header">
          <div className="breadcrumb">
            <span>Workspace</span>
            <ChevronDown size={12} />
            <strong>{project?.name ?? 'Twoje projekty'}</strong>
          </div>
          <div className="header-actions">
            <span className={'connection-state ' + (live ? 'live' : '')}>
              <span className={'status-dot ' + (live ? 'good' : '')} />
              {live ? 'Pamięć na żywo' : 'Łączenie pamięci'}
            </span>
            <button className="button subtle compact" onClick={() => openDialog('sessions')}>
              <Layers3 size={14} />
              {activeRuns.length} sesji
            </button>
            <IconButton label="Ustawienia" onClick={() => openDialog('settings')}>
              <Settings2 size={17} />
            </IconButton>
          </div>
        </header>
        {project ? (
          <>
            <div className="project-toolbar">
              <div>
                <h1>
                  {project.name}
                  <span className="project-tag">LOCAL</span>
                </h1>
                <div className="worktree-selector">
                  <Folder size={12} />
                  <select
                    aria-label="Katalog pracy projektu"
                    value={worktree}
                    onChange={(e) => {
                      setWorktree(e.target.value);
                      setLaunchRequested(true);
                      setSelectedNode(undefined);
                    }}
                  >
                    {(project.roots ?? [project.root]).map((path) => (
                      <option key={path} value={path}>
                        {path}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="project-toolbar-actions">
                {project.memory_snapshot && (
                  <button
                    className="button subtle"
                    onClick={() =>
                      openImport(
                        typeof project.memory_snapshot === 'string'
                          ? project.memory_snapshot
                          : project.root + '/.memory/devbrain',
                        project.root,
                      )
                    }
                  >
                    <ArrowDownToLine size={13} />
                    Wczytaj snapshot
                  </button>
                )}
                <button className="button subtle" onClick={() => void launch(true)}>
                  <RefreshCw size={13} />
                  Połącz klientów
                </button>
                <button className="button" onClick={() => openDialog('settings')}>
                  <Plus size={14} />
                  Dostosuj agentów
                </button>
              </div>
            </div>
            {error && !dialog && (
              <div className="workspace-notice" role="status">
                <CircleAlert size={15} />
                <span>{error}</span>
                <button onClick={() => setError('')} aria-label="Ukryj komunikat">
                  <X size={13} />
                </button>
              </div>
            )}
            <div className="workspace-grid">
              <div
                className="agent-grid"
                style={{
                  gridTemplateColumns: `repeat(${Math.max(1, slots.length)},minmax(310px,1fr))`,
                }}
              >
                {slots.map((slot) => {
                  const profile = state.profiles.find((item) => item.id === slot.profile_id);
                  return (
                    <div className="agent-slot" key={project.id + ':' + slot.id}>
                      <div className="slot-profile-bar">
                        <span>
                          <span className="slot-number">{slots.indexOf(slot) + 1}</span>
                          {profile?.label ?? slot.profile_id}
                        </span>
                        <div>
                          <button onClick={() => showAuth(profile?.client ?? 'codex')}>
                            <LogIn size={12} />
                            {statusLabel(auth[profile?.client ?? 'codex'] ?? {}).title}
                          </button>
                          {slots.length > 1 && (
                            <IconButton
                              label="Ukryj panel (proces pozostaje w tle)"
                              onClick={() =>
                                void saveSlots(slots.filter((item) => item.id !== slot.id))
                              }
                            >
                              <X size={12} />
                            </IconButton>
                          )}
                        </div>
                      </div>
                      <TerminalPanel
                        run={clientRun(slot)}
                        profile={profile}
                        onRefresh={refreshState}
                        onLogin={() => showAuth(profile?.client ?? 'codex')}
                        onResume={() => void launchSlot(slot, true)}
                        onNewSession={() => void launchSlot(slot, false)}
                      />
                    </div>
                  );
                })}
              </div>
              <div className="lower-grid">
                <Inspector
                  key={project.id}
                  project={project}
                  worktree={worktree}
                  refresh={refresh}
                  selectedNode={selectedNode}
                  onChange={change}
                />
                <BrainGraph
                  project={project.id}
                  worktree={worktree}
                  refresh={refresh}
                  onSelect={(node) => {
                    if (node.kind === 'project') {
                      const id = label(node.project_id) || node.id.replace(/^project:/, '');
                      const target = state.projects.find((item) => item.id === id);
                      if (target) {
                        setLaunchRequested(false);
                        setSelected(target.id);
                        setWorktree(target.root);
                        setSelectedNode(undefined);
                        setError('');
                      } else setError('Projekt nie jest już dostępny. Odśwież listę projektów.');
                    } else if (
                      node.entity_type === 'inbox_group' ||
                      node.entity_type === 'inbox' ||
                      node.id.startsWith('inbox:')
                    ) {
                      setInboxId(
                        node.entity_type === 'inbox_group'
                          ? undefined
                          : label(node.inbox_id) || node.id.replace(/^inbox:/, ''),
                      );
                      openDialog('inbox');
                    } else setSelectedNode({ ...node, selection: Date.now() });
                  }}
                />
              </div>
            </div>
          </>
        ) : (
          <div className="welcome">
            <div className="welcome-symbol">
              <BrainCircuit size={52} />
            </div>
            <span className="eyebrow">JEDNO MIEJSCE DLA CAŁEJ PRACY</span>
            <h1>
              Twoje projekty.
              <br />
              Wspólna pamięć.
            </h1>
            <p>
              Otwórz projekt, pracuj z Codexem i Claude’em.
              <br />
              Ustalenia, zadania i źródła pozostają z Tobą.
            </p>
            <div className="form-actions">
              <button className="button primary" onClick={() => openDialog('project')}>
                <FolderPlus size={16} />
                Dodaj projekt
              </button>
              <button className="button" onClick={() => openDialog('discovery')}>
                <FolderSearch size={16} />
                Znajdź projekty
              </button>
            </div>
            <button className="text-button" onClick={() => openImport()}>
              Mam już projekt z zapisaną pamięcią <ArrowUpRight size={13} />
            </button>
          </div>
        )}
        <footer className="workspace-footer">
          <span>
            <BrainCircuit size={12} />
            Wiedza pozostaje na tym komputerze
          </span>
          <span>Oryginalne CLI · natywne uprawnienia · trwała pamięć</span>
          <span>
            <Activity size={12} />
            {activeRuns.length} aktywnych procesów
          </span>
        </footer>
      </main>
      {dialog && (
        <Modal
          title={
            dialog === 'project'
              ? 'Dodaj projekt'
              : dialog === 'discovery'
                ? 'Znajdź projekty'
                : dialog === 'settings'
                  ? 'Ustawienia środowiska'
                  : dialog === 'sessions'
                    ? 'Sesje na tym komputerze'
                    : dialog === 'inbox'
                      ? 'Nieprzypisane wpisy'
                      : dialog === 'auth'
                        ? 'Połączenie z ' + (authClient === 'codex' ? 'Codex CLI' : 'Claude Code')
                        : 'Wczytaj pamięć projektu'
          }
          wide={dialog === 'auth' && !!authRun}
          onClose={() => {
            setDialog(null);
            if (dialog === 'auth') void refreshAuth();
          }}
        >
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {dialog === 'project' && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void register(projectName, projectRoot);
              }}
            >
              <p className="hint">
                Wskaż istniejący katalog w Ubuntu / WSL. Projekt będzie dostępny z każdego miejsca,
                w którym uruchomisz devbrain.
              </p>
              <label>
                Nazwa projektu
                <input
                  autoFocus
                  required
                  value={projectName}
                  onChange={(e) => {
                    setProjectName(e.target.value);
                    setDetectedProject(null);
                  }}
                  placeholder="moja-aplikacja"
                />
              </label>
              <label>
                Katalog projektu
                <input
                  required
                  value={projectRoot}
                  onChange={(e) => {
                    setProjectRoot(e.target.value);
                    setDetectedProject(null);
                  }}
                  placeholder="/home/…/src/moja-aplikacja"
                />
              </label>
              {detectedProject && (
                <div className="snapshot-offer">
                  <strong>W tym katalogu jest przenośna pamięć.</strong>
                  <p>
                    Wczytaj ją, aby zachować tożsamość projektu, plany i historię. Samo wykrycie
                    niczego nie importuje.
                  </p>
                  <div className="form-actions">
                    <button
                      type="button"
                      className="button primary"
                      onClick={() => openImport(detectedProject.path, detectedProject.root)}
                    >
                      Sprawdź i wczytaj pamięć
                    </button>
                    <button
                      type="button"
                      className="button subtle"
                      onClick={() =>
                        void register(detectedProject.name, detectedProject.root, true)
                      }
                    >
                      Zarejestruj jako nowy projekt
                    </button>
                  </div>
                  <p className="hint">
                    Nowy projekt otrzyma inną tożsamość i nie odzyska pamięci z tego snapshotu.
                  </p>
                </div>
              )}
              <div className="form-actions">
                <button className="button primary" disabled={busy || !projectName || !projectRoot}>
                  {busy ? (
                    <Busy />
                  ) : (
                    <>
                      <FolderPlus size={15} />
                      Dodaj projekt
                    </>
                  )}
                </button>
              </div>
            </form>
          )}
          {dialog === 'discovery' && (
            <div>
              <p className="hint">
                DevBrain przeszuka wyłącznie wskazane katalogi. Możesz dodać ścieżki Ubuntu i
                Windows dostępne w WSL.
              </p>
              <label>
                Katalogi do przeszukania, po jednym w wierszu
                <textarea
                  value={scanRoots}
                  onChange={(e) => setScanRoots(e.target.value)}
                  placeholder={'/home/…/src\n/mnt/c/Users/…/Projects'}
                  rows={4}
                />
              </label>
              <div className="form-actions">
                <button
                  className="button primary"
                  onClick={() => {
                    setError('');
                    void post<Json>('/api/discovery', {
                      roots: scanRoots
                        .split('\n')
                        .map((value) => value.trim())
                        .filter(Boolean),
                    })
                      .then(setDiscovery)
                      .catch((err) => setError(message(err)));
                  }}
                >
                  <FolderSearch size={15} />
                  Szukaj projektów
                </button>
                {['running', 'pending'].includes(label(discovery.state)) && (
                  <button
                    className="button"
                    onClick={() =>
                      void api('/api/discovery', { method: 'DELETE' })
                        .then((value) => setDiscovery(value))
                        .catch((err) => setError(message(err)))
                    }
                  >
                    Zatrzymaj
                  </button>
                )}
              </div>
              <div className="discovery-status">
                {label(discovery.state)} {label(discovery.scanned_count)}
                {Boolean(discovery.truncated) && ' · osiągnięto limit skanowania'}
              </div>
              <div className="discovery-list">
                {entries(discovery.candidates ?? discovery.projects ?? discovery.results).map(
                  (item) => (
                    <div key={label(item.root ?? item.path)}>
                      <Folder size={16} />
                      <span>
                        <strong>{label(item.name)}</strong>
                        <small>{label(item.root ?? item.path)}</small>
                      </span>
                      <button
                        className="button compact"
                        disabled={busy || !!item.registered}
                        onClick={() =>
                          item.memory_snapshot
                            ? openImport(
                                typeof item.memory_snapshot === 'string'
                                  ? item.memory_snapshot
                                  : label(item.root ?? item.path) + '/.memory/devbrain',
                                label(item.root ?? item.path),
                              )
                            : void register(
                                label(item.name) ||
                                  label(item.root ?? item.path)
                                    .split('/')
                                    .pop() ||
                                  'projekt',
                                label(item.root ?? item.path),
                              )
                        }
                      >
                        {item.registered ? (
                          <Check size={13} />
                        ) : item.memory_snapshot ? (
                          'Wczytaj pamięć'
                        ) : (
                          'Dodaj'
                        )}
                      </button>
                    </div>
                  ),
                )}
              </div>
              {Boolean(discovery.errors) && <SmallData data={discovery.errors} />}
            </div>
          )}
          {dialog === 'settings' && (
            <div className="settings-form">
              <p className="hint">
                Wybierz oryginalnych klientów oraz układ paneli. Ukrycie panelu zachowuje działającą
                sesję w tle.
              </p>
              <h3>Profile klientów</h3>
              {draftProfiles.map((profile, index) => (
                <div className="profile-editor" key={profile.id}>
                  <input
                    aria-label={'Nazwa profilu ' + profile.id}
                    value={profile.label}
                    onChange={(e) =>
                      setDraftProfiles((values) =>
                        values.map((item, i) =>
                          i === index ? { ...item, label: e.target.value } : item,
                        ),
                      )
                    }
                  />
                  <select
                    aria-label={'Klient profilu ' + profile.id}
                    value={profile.client}
                    onChange={(e) =>
                      setDraftProfiles((values) =>
                        values.map((item, i) =>
                          i === index
                            ? { ...item, client: e.target.value as Profile['client'] }
                            : item,
                        ),
                      )
                    }
                  >
                    <option value="codex">Codex CLI</option>
                    <option value="claude">Claude Code</option>
                  </select>
                  <input
                    aria-label={'Model profilu ' + profile.id}
                    value={profile.model ?? ''}
                    placeholder="Model domyślny klienta"
                    onChange={(e) =>
                      setDraftProfiles((values) =>
                        values.map((item, i) =>
                          i === index ? { ...item, model: e.target.value || null } : item,
                        ),
                      )
                    }
                  />
                </div>
              ))}
              <button
                className="text-button"
                onClick={() =>
                  setDraftProfiles((values) => [
                    ...values,
                    {
                      id: 'profile-' + crypto.randomUUID().slice(0, 8),
                      label: 'Nowy profil',
                      client: 'codex',
                    },
                  ])
                }
              >
                <Plus size={13} />
                Dodaj profil
              </button>
              <h3>Układ agentów</h3>
              <div className="segmented">
                <button
                  className={presetScope === 'project' ? 'active' : ''}
                  disabled={!project}
                  onClick={() => {
                    setPresetScope('project');
                    setDraftSlots(slots.map((item) => ({ ...item })));
                  }}
                >
                  Ten projekt
                </button>
                <button
                  className={presetScope === 'global' ? 'active' : ''}
                  onClick={() => {
                    setPresetScope('global');
                    setDraftSlots(state.settings.default_slots.map((item) => ({ ...item })));
                  }}
                >
                  Domyślny
                </button>
              </div>
              {draftSlots.map((slot, index) => (
                <div className="profile-editor" key={slot.id}>
                  <span className="slot-number">{index + 1}</span>
                  <select
                    aria-label={'Profil panelu ' + (index + 1)}
                    value={slot.profile_id}
                    onChange={(e) =>
                      setDraftSlots((values) =>
                        values.map((item, i) =>
                          i === index ? { ...item, profile_id: e.target.value } : item,
                        ),
                      )
                    }
                  >
                    {draftProfiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.label}
                      </option>
                    ))}
                  </select>
                  <IconButton
                    label={'Usuń panel ' + (index + 1)}
                    disabled={draftSlots.length <= 1}
                    onClick={() =>
                      setDraftSlots((values) => values.filter((item) => item.id !== slot.id))
                    }
                  >
                    <X size={14} />
                  </IconButton>
                </div>
              ))}
              <button
                className="text-button"
                disabled={draftSlots.length >= 8 || !draftProfiles.length}
                onClick={() =>
                  setDraftSlots((values) => [
                    ...values,
                    {
                      id: 'slot-' + crypto.randomUUID().slice(0, 8),
                      profile_id: draftProfiles[0].id,
                    },
                  ])
                }
              >
                <Plus size={13} />
                Dodaj panel
              </button>
              <div className="form-actions">
                <button
                  className="button primary"
                  disabled={busy || !draftSlots.length}
                  onClick={() => void settingsSave()}
                >
                  Zapisz ustawienia
                </button>
              </div>
            </div>
          )}
          {dialog === 'sessions' && (
            <div className="sessions-list">
              <p className="hint">
                Zamknięcie przeglądarki odłącza widok. Procesy działają, dopóki ich nie zatrzymasz
                lub nie zakończysz WSL.
              </p>
              {state.runs.map((run) => (
                <div key={run.id}>
                  <SquareTerminal size={19} />
                  <span>
                    <strong>
                      {run.client} ·{' '}
                      {state.projects.find((item) => item.id === run.project_id)?.name ??
                        'logowanie'}
                    </strong>
                    <small>{run.worktree ?? 'Sesja operacyjna'}</small>
                    <small>
                      {run.lifecycle ?? run.state} · generacja {run.generation}
                    </small>
                  </span>
                  {run.project_id && (
                    <button
                      className="button compact"
                      onClick={() => {
                        setSelected(run.project_id!);
                        setWorktree(run.worktree ?? '');
                        setLaunchRequested(false);
                        setDialog(null);
                      }}
                    >
                      Przejdź
                    </button>
                  )}
                  <button
                    className="button compact"
                    onClick={() =>
                      void post(`/api/client-runs/${encodeURIComponent(run.id)}/stop`, {
                        generation: run.generation,
                      })
                        .then(() => refreshState())
                        .catch((err) => setError(message(err)))
                    }
                  >
                    Zatrzymaj
                  </button>
                </div>
              ))}
              {!state.runs.length && <Empty title="Brak uruchomionych sesji" />}
            </div>
          )}
          {dialog === 'inbox' && <InboxBrowser initialId={inboxId} />}
          {dialog === 'import' && (
            <MemoryImportForm
              root={importRoot}
              initialPath={importPath}
              onDone={(result) => {
                setDialog(null);
                const imported = object(result?.project);
                if (imported.id) {
                  setSelected(label(imported.id));
                  setWorktree(label(imported.root));
                  setLaunchRequested(false);
                }
                change();
              }}
            />
          )}
          {dialog === 'auth' && (
            <div>
              <div className="auth-summary">
                <span className={'client-mark ' + authClient}>⌘</span>
                <div>
                  <strong>{authClient === 'codex' ? 'Codex CLI' : 'Claude Code'}</strong>
                  <p>
                    <Badge tone={statusLabel(auth[authClient] ?? {}).tone}>
                      {statusLabel(auth[authClient] ?? {}).title}
                    </Badge>
                  </p>
                </div>
                <button className="button compact" onClick={() => void refreshAuth()}>
                  <RefreshCw size={13} />
                  Sprawdź stan
                </button>
              </div>
              <p className="hint">
                Logowanie odbywa się w oryginalnym kliencie. Ten terminal operacyjny nie trafia do
                wspólnej pamięci ani grafu. Stan logowania nie potwierdza dostępnego limitu.
              </p>
              {authRun ? (
                <div className="auth-terminal">
                  <TerminalPanel
                    run={authRun}
                    profile={{
                      id: authClient,
                      client: authClient as Profile['client'],
                      label: authClient === 'codex' ? 'Codex' : 'Claude',
                    }}
                    operational
                    onRefresh={() => void refreshAuth()}
                  />
                </div>
              ) : (
                <div className="form-actions">
                  <button
                    className="button primary"
                    disabled={busy || statusLabel(auth[authClient] ?? {}).title === 'Brak programu'}
                    onClick={() => void startAuth()}
                  >
                    <LogIn size={15} />
                    Otwórz logowanie
                  </button>
                </div>
              )}
              <SmallData data={auth[authClient]} />
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
