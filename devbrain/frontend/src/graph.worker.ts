/// <reference lib="webworker" />
import type { GraphEdge } from './types';
interface Point {
  id: string;
  x: number;
  y: number;
  z: number;
  vx: number;
  vy: number;
  vz: number;
}
const points = new Map<string, Point>();
let ids: string[] = [];
let edges: GraphEdge[] = [];
let running = true;
function seed(id: string): number {
  let hash = 2166136261;
  for (const char of id) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return (hash >>> 0) / 4294967295;
}
self.onmessage = (event: MessageEvent) => {
  const data = event.data;
  if (data.type === 'pause') {
    running = !data.paused;
    return;
  }
  if (data.type === 'data') {
    ids = (data.ids as string[]).slice(0, 2000);
    edges = (data.edges as GraphEdge[]).slice(0, 5000);
    const alive = new Set(ids);
    for (const id of points.keys()) if (!alive.has(id)) points.delete(id);
    for (const id of ids)
      if (!points.has(id)) {
        const a = seed(id) * Math.PI * 2,
          b = seed(id + 'z') * 2 - 1,
          r = 20 + seed(id + 'r') * 50;
        points.set(id, {
          id,
          x: Math.cos(a) * r,
          y: Math.sin(a) * r,
          z: b * r,
          vx: 0,
          vy: 0,
          vz: 0,
        });
      }
    publish();
  }
};
function publish() {
  const positions = new Float32Array(ids.length * 3);
  ids.forEach((id, index) => {
    const p = points.get(id)!;
    positions[index * 3] = p.x;
    positions[index * 3 + 1] = p.y;
    positions[index * 3 + 2] = p.z;
  });
  self.postMessage({ type: 'positions', ids, positions }, [positions.buffer]);
}
setInterval(() => {
  if (!running || !ids.length) return;
  const cells = new Map<string, Point[]>(),
    size = 16;
  for (const p of points.values()) {
    const key = [Math.floor(p.x / size), Math.floor(p.y / size), Math.floor(p.z / size)].join(',');
    const cell = cells.get(key) ?? [];
    cell.push(p);
    cells.set(key, cell);
    p.vx -= p.x * 0.0004;
    p.vy -= p.y * 0.0004;
    p.vz -= p.z * 0.0004;
  }
  for (const p of points.values()) {
    const x = Math.floor(p.x / size),
      y = Math.floor(p.y / size),
      z = Math.floor(p.z / size);
    let considered = 0;
    for (let dx = -1; dx <= 1; dx++)
      for (let dy = -1; dy <= 1; dy++)
        for (let dz = -1; dz <= 1; dz++)
          for (const q of cells.get([x + dx, y + dy, z + dz].join(',')) ?? []) {
            if (p === q || considered++ > 90) continue;
            const ax = p.x - q.x,
              ay = p.y - q.y,
              az = p.z - q.z,
              d = ax * ax + ay * ay + az * az + 2,
              f = 0.35 / d;
            p.vx += ax * f;
            p.vy += ay * f;
            p.vz += az * f;
          }
  }
  for (const edge of edges) {
    const a = points.get(edge.from_id),
      b = points.get(edge.to_id);
    if (!a || !b) continue;
    const x = b.x - a.x,
      y = b.y - a.y,
      z = b.z - a.z;
    const d = Math.sqrt(x * x + y * y + z * z) || 1,
      f = (d - 12) * 0.0006;
    a.vx += x * f;
    a.vy += y * f;
    a.vz += z * f;
    b.vx -= x * f;
    b.vy -= y * f;
    b.vz -= z * f;
  }
  for (const p of points.values()) {
    p.vx *= 0.86;
    p.vy *= 0.86;
    p.vz *= 0.86;
    p.x += Math.max(-2, Math.min(2, p.vx));
    p.y += Math.max(-2, Math.min(2, p.vy));
    p.z += Math.max(-2, Math.min(2, p.vz));
  }
  publish();
}, 50);
