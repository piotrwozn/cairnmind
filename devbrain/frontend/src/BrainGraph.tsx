import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { Expand, Globe2, List, Pause, Play, RotateCcw } from 'lucide-react';
import { api, message, query } from './api';
import { Badge, Empty, IconButton, label, Modal } from './components';
import type { GraphData, GraphNode, Json } from './types';
const colors: Record<string, string> = {
  fact: '#b9d791',
  decision: '#a9bbeb',
  constraint: '#dfb98b',
  task: '#dfbc78',
  source: '#85c7c1',
  message: '#85c7c1',
  technical: '#667d87',
  revision: '#8e9caf',
  unassigned: '#d0ac83',
  transcript: '#85c7c1',
  plan: '#a1b5da',
  file: '#8a9aa9',
  symbol: '#bac6d1',
  preference: '#b9d791',
  question: '#ccaccc',
  checkpoint: '#90b9d4',
};
export function nodeColor(kind: string) {
  return colors[kind] ?? '#9aafbb';
}
export function BrainGraph({
  project,
  worktree,
  refresh,
  onSelect,
}: {
  project?: string;
  worktree?: string;
  refresh: number;
  onSelect: (node: GraphNode) => void;
}) {
  const selectedRef = useRef(''),
    pausedRef = useRef(false),
    controlsRef = useRef<OrbitControls | null>(null),
    born = useRef(new Map<string, number>());
  const host = useRef<HTMLDivElement>(null),
    worker = useRef<Worker | null>(null),
    mesh = useRef<THREE.InstancedMesh | null>(null),
    line = useRef<THREE.LineSegments | null>(null),
    camera = useRef<THREE.PerspectiveCamera | null>(null),
    dataRef = useRef<GraphData>({ nodes: [], edges: [] }),
    positions = useRef(new Map<string, THREE.Vector3>()),
    selectRef = useRef(onSelect);
  const [data, setData] = useState<GraphData>({ nodes: [], edges: [] }),
    [global, setGlobal] = useState(false),
    [group, setGroup] = useState(''),
    [pageCursor, setPageCursor] = useState(''),
    [groupsOpen, setGroupsOpen] = useState(false),
    [groupItems, setGroupItems] = useState<Json[]>([]),
    [groupsCursor, setGroupsCursor] = useState<string | null>(null),
    [error, setError] = useState(''),
    [fallback, setFallback] = useState(false),
    [list, setList] = useState(false),
    [selected, setSelected] = useState(''),
    [search, setSearch] = useState('');
  const reduced = useRef(window.matchMedia('(prefers-reduced-motion: reduce)').matches),
    [paused, setPaused] = useState(reduced.current);
  useEffect(() => {
    if (global && groupsOpen) {
      setGroupItems(
        data.groups ??
          data.nodes.filter(
            (node) => node.kind === 'project' || node.entity_type === 'inbox_group',
          ),
      );
      setGroupsCursor(null);
    }
  }, [data, global, groupsOpen]);
  useEffect(() => {
    selectRef.current = onSelect;
  }, [onSelect]);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  useEffect(() => {
    setPageCursor('');
  }, [project, worktree, global, group]);
  useEffect(() => {
    let alive = true;
    void api<GraphData>(
      '/api/inspector/graph' +
        query({
          project: global ? undefined : project,
          worktree: global ? undefined : worktree,
          group,
          page_cursor: pageCursor,
          limit: 2000,
        }),
    )
      .then((value) => {
        if (alive) {
          setData({
            ...value,
            nodes: (value.nodes ?? []).slice(0, 2000),
            edges: (value.edges ?? []).slice(0, 5000),
          });
          setError('');
        }
      })
      .catch((err) => {
        if (alive) setError(message(err));
      });
    return () => {
      alive = false;
    };
  }, [project, worktree, refresh, global, group, pageCursor]);
  useEffect(() => {
    if (!host.current) return;
    const element = host.current;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true,
        powerPreference: 'low-power',
      });
    } catch {
      setFallback(true);
      return;
    }
    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(45, 1, 0.1, 1500);
    cam.position.set(0, 8, 180);
    camera.current = cam;
    renderer.setPixelRatio(Math.min(devicePixelRatio, 1.6));
    renderer.setClearColor('#0d1317', 0);
    element.appendChild(renderer.domElement);
    renderer.domElement.setAttribute('aria-label', 'Interaktywny graf pamięci 3D');
    renderer.domElement.setAttribute('role', 'img');
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 18;
    controls.maxDistance = 500;
    controls.autoRotate = false;
    controlsRef.current = controls;
    const spheres = new THREE.SphereGeometry(1, 8, 6),
      material = new THREE.MeshBasicMaterial({ color: '#ffffff' }),
      nodes = new THREE.InstancedMesh(spheres, material, 2000);
    nodes.count = 0;
    nodes.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    scene.add(nodes);
    mesh.current = nodes;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(5000 * 6), 3));
    geometry.setDrawRange(0, 0);
    const lines = new THREE.LineSegments(
      geometry,
      new THREE.LineBasicMaterial({ color: '#6d9199', transparent: true, opacity: 0.19 }),
    );
    scene.add(lines);
    line.current = lines;
    const sim = new Worker(new URL('./graph.worker.ts', import.meta.url), { type: 'module' });
    worker.current = sim;
    sim.onmessage = (event: MessageEvent<{ ids: string[]; positions: Float32Array }>) => {
      const packet = event.data;
      const next = new Map<string, THREE.Vector3>();
      packet.ids.forEach((id, index) =>
        next.set(
          id,
          new THREE.Vector3(
            packet.positions[index * 3],
            packet.positions[index * 3 + 1],
            packet.positions[index * 3 + 2],
          ),
        ),
      );
      positions.current = next;
    };
    sim.postMessage({
      type: 'data',
      ids: dataRef.current.nodes.map((node) => node.id),
      edges: dataRef.current.edges,
    });
    sim.postMessage({ type: 'pause', paused: reduced.current });
    const matrix = new THREE.Object3D(),
      color = new THREE.Color(),
      raycaster = new THREE.Raycaster(),
      mouse = new THREE.Vector2();
    let frame = 0;
    const resize = () => {
      const width = element.clientWidth,
        height = element.clientHeight;
      if (!width || !height) return;
      renderer.setSize(width, height);
      cam.aspect = width / height;
      cam.updateProjectionMatrix();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    const click = (event: MouseEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.set(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        (-(event.clientY - rect.top) / rect.height) * 2 + 1,
      );
      raycaster.setFromCamera(mouse, cam);
      const hit = raycaster.intersectObject(nodes)[0];
      if (hit?.instanceId !== undefined) {
        const node = dataRef.current.nodes[hit.instanceId];
        if (node) {
          setSelected(node.id);
          if (node.kind === 'project') {
            setGlobal(false);
            setGroup('');
            setPageCursor('');
            selectRef.current(node);
          } else if (node.kind === 'group') {
            setGroup(String(node.group ?? node.id));
            if (node.entity_type === 'inbox_group') selectRef.current(node);
          } else selectRef.current(node);
        }
      }
    };
    renderer.domElement.addEventListener('click', click);
    const render = () => {
      frame = requestAnimationFrame(render);
      const current = dataRef.current;
      nodes.count = current.nodes.length;
      current.nodes.forEach((node, index) => {
        const point = positions.current.get(node.id);
        if (!point) return;
        matrix.position.copy(point);
        const base =
          node.kind === 'plan'
            ? 1.6
            : node.kind === 'source' || node.kind === 'transcript'
              ? 0.8
              : 1.2;
        const age = (performance.now() - (born.current.get(node.id) ?? 0)) / 550;
        const appear = reduced.current || pausedRef.current ? 1 : Math.max(0.08, Math.min(1, age));
        matrix.scale.setScalar(base * appear * (selectedRef.current === node.id ? 1.8 : 1));
        matrix.updateMatrix();
        nodes.setMatrixAt(index, matrix.matrix);
        color.set(selectedRef.current === node.id ? '#e5efcf' : nodeColor(node.kind));
        nodes.setColorAt(index, color);
      });
      nodes.instanceMatrix.needsUpdate = true;
      if (nodes.instanceColor) nodes.instanceColor.needsUpdate = true;
      const array = geometry.attributes.position.array as Float32Array;
      let count = 0;
      for (const edge of current.edges) {
        const a = positions.current.get(edge.from_id),
          b = positions.current.get(edge.to_id);
        if (!a || !b || count >= 5000) continue;
        array.set([a.x, a.y, a.z, b.x, b.y, b.z], count * 6);
        count++;
      }
      geometry.setDrawRange(0, count * 2);
      geometry.attributes.position.needsUpdate = true;
      controls.update();
      renderer.render(scene, cam);
    };
    render();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      sim.terminate();
      worker.current = null;
      controls.dispose();
      renderer.domElement.removeEventListener('click', click);
      spheres.dispose();
      material.dispose();
      geometry.dispose();
      (lines.material as THREE.Material).dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, []);
  useEffect(() => {
    dataRef.current = data;
    for (const node of data.nodes)
      if (!born.current.has(node.id)) born.current.set(node.id, performance.now());
    worker.current?.postMessage({
      type: 'data',
      ids: data.nodes.map((node) => node.id),
      edges: data.edges,
    });
  }, [data]);
  useEffect(() => {
    pausedRef.current = paused;
    worker.current?.postMessage({ type: 'pause', paused });
  }, [paused]);
  const visible = data.nodes.filter(
    (node) =>
      !search ||
      `${node.label ?? ''} ${node.kind} ${node.id}`.toLowerCase().includes(search.toLowerCase()),
  );
  const loadGroups = async (cursor?: string) => {
    if (global) {
      setGroupItems(
        data.groups ??
          data.nodes.filter(
            (node) => node.kind === 'project' || node.entity_type === 'inbox_group',
          ),
      );
      setGroupsCursor(null);
      return;
    }
    try {
      const page = await api<{ items: Json[]; next_cursor?: string }>(
        '/api/inspector/graph/groups' +
          query({ project: global ? undefined : project, cursor, limit: 100 }),
      );
      setGroupItems((old) => (cursor ? [...old, ...page.items] : page.items));
      setGroupsCursor(page.next_cursor ?? null);
    } catch (err) {
      setError(message(err));
    }
  };
  const choose = (node: GraphNode) => {
    setSelected(node.id);
    if (node.kind === 'project') {
      setGlobal(false);
      setGroup('');
      setPageCursor('');
      onSelect(node);
    } else if (node.kind === 'group') {
      setGroup(String(node.group ?? node.id));
      if (node.entity_type === 'inbox_group') onSelect(node);
    } else onSelect(node);
  };
  return (
    <section className="brain-panel panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">LIVE MEMORY</span>
          <h2>
            Mózg projektu <span className="live-indicator" />
          </h2>
        </div>
        <div className="heading-actions">
          <IconButton
            label={global ? 'Pokaż wybrany projekt' : 'Pokaż wszystkie projekty'}
            onClick={() => {
              setGlobal((value) => !value);
              setGroup('');
            }}
          >
            <Globe2 size={15} />
          </IconButton>
          <IconButton
            label={paused ? 'Wznów animację' : 'Wstrzymaj animację'}
            onClick={() => setPaused((value) => !value)}
          >
            {paused ? <Play size={14} /> : <Pause size={14} />}
          </IconButton>
          <IconButton label="Widok listy" onClick={() => setList((value) => !value)}>
            <List size={16} />
          </IconButton>
          <IconButton
            label="Przywróć kamerę"
            onClick={() => {
              camera.current?.position.set(0, 8, 180);
              controlsRef.current?.target.set(0, 0, 0);
            }}
          >
            <Expand size={15} />
          </IconButton>
        </div>
      </div>
      <div className="graph-toolbar">
        <Badge tone="good">{global ? 'Wszystkie projekty' : 'Projekt'}</Badge>
        <button
          onClick={() => {
            setGroupsOpen(true);
            void loadGroups();
          }}
        >
          Grupy
        </button>
        <input
          aria-label="Znajdź węzeł grafu"
          placeholder="Znajdź w widoku…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {group && (
          <button onClick={() => setGroup('')}>
            <RotateCcw size={12} />
            Grupy
          </button>
        )}
      </div>
      <div className={'graph-stage ' + (list || fallback ? 'list-mode' : '')}>
        <div ref={host} className="graph-canvas" />
        {(list || fallback) && (
          <div className="graph-list">
            {fallback && <p className="hint">Graf w trybie listy — WebGL jest niedostępny.</p>}
            {visible.map((node) => (
              <button
                key={node.id}
                className={selected === node.id ? 'selected' : ''}
                onClick={() => choose(node)}
              >
                <span className="node-dot" style={{ background: nodeColor(node.kind) }} />
                <span>{node.label ?? node.id}</span>
                <small>{node.kind}</small>
              </button>
            ))}
          </div>
        )}
        {search && !list && !fallback && (
          <div className="graph-search-results">
            {visible.slice(0, 20).map((node) => (
              <button key={node.id} onClick={() => choose(node)}>
                <span className="node-dot" style={{ background: nodeColor(node.kind) }} />
                {node.label ?? node.id}
                <small>{node.kind}</small>
              </button>
            ))}
            {!visible.length && <p>Brak pasujących węzłów w tym widoku.</p>}
          </div>
        )}
        {!data.nodes.length && !error && (
          <div className="graph-empty">
            <Empty title="Tutaj pojawią się powiązania">
              Źródła, decyzje i zadania dodawane do pamięci utworzą graf.
            </Empty>
          </div>
        )}
        {error && (
          <div className="graph-notice" role="status">
            {error}
          </div>
        )}
        {!!data.groups?.length && (
          <div className="graph-groups">
            {data.groups.slice(0, 8).map((item, index) => (
              <button key={label(item.id) || index} onClick={() => choose(item as GraphNode)}>
                {label(item.label) || label(item.kind) || 'Grupa'} <span>{label(item.count)}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="graph-footer">
        <span>
          <i style={{ background: colors.fact }} />
          ustalenia <i style={{ background: colors.task }} />
          zadania <i style={{ background: colors.source }} />
          źródła
        </span>
        <span>
          {pageCursor && <button onClick={() => setPageCursor('')}>Od początku</button>}
          {data.next_cursor && (
            <button onClick={() => setPageCursor(data.next_cursor!)}>Następny fragment</button>
          )}
          {data.nodes.length.toLocaleString()} węzłów · {data.edges.length.toLocaleString()} relacji
          {data.truncated ? ' · widok grupowany' : ''}
        </span>
      </div>
      {groupsOpen && (
        <Modal title="Grupy pamięci" onClose={() => setGroupsOpen(false)}>
          <div className="record-list">
            {groupItems.map((item, index) => (
              <button
                className="record-row"
                key={label(item.id) || index}
                onClick={() => {
                  choose(item as GraphNode);
                  setGroupsOpen(false);
                }}
              >
                <span className="record-row-content">
                  <span>{label(item.label) || label(item.group) || label(item.id)}</span>
                  <small>{label(item.count)} węzłów</small>
                </span>
              </button>
            ))}
            {groupsCursor && (
              <button className="load-more" onClick={() => void loadGroups(groupsCursor)}>
                Dalsze grupy
              </button>
            )}
            {!groupItems.length && <Empty title="Brak grup w tym zakresie" />}
          </div>
        </Modal>
      )}
    </section>
  );
}
