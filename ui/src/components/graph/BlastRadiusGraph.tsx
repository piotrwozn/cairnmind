import { useCallback, useMemo } from 'react';
import dagre from '@dagrejs/dagre';
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  type NodeMouseHandler,
} from '@xyflow/react';
import { Crosshair, GitFork, RotateCcw } from 'lucide-react';
import { GlassBadge } from '@/components/glass/GlassBadge';
import {
  AnimatedStatusEdge,
  type CairnmindFlowEdge,
} from '@/components/graph/AnimatedStatusEdge';
import {
  GlassFlowNode,
  type CairnmindFlowNode,
} from '@/components/graph/GlassFlowNode';
import type { DependencyGraph, GraphNodeData } from '@/types/contracts';

interface BlastRadiusGraphProps {
  graph: DependencyGraph;
  blastRootId: string | null;
  onSelectBlastRoot: (nodeId: string | null) => void;
  onInspectNode: (node: GraphNodeData) => void;
  onlyStaleAndConflicts: boolean;
}

const NODE_WIDTH = 228;
const NODE_HEIGHT = 64;
const COLUMN_X_STEP = 296;

const COLUMN_LABELS = [
  '1 · Source Files',
  '2 · Evidence (Quotes & Tests)',
  '3 · Memory Claims',
  '4 · Plan Tasks',
];

const nodeTypes = { glassNode: GlassFlowNode };
const edgeTypes = { statusEdge: AnimatedStatusEdge };

/**
 * Compute all downstream node and edge IDs reachable from `rootId`.
 */
export function computeBlastRadius(
  graph: DependencyGraph,
  rootId: string | null,
): { nodes: Set<string>; edges: Set<string> } {
  const visitedNodes = new Set<string>();
  const visitedEdges = new Set<string>();
  if (!rootId) return { nodes: visitedNodes, edges: visitedEdges };

  const outgoing = new Map<string, Array<{ edgeId: string; target: string }>>();
  for (const edge of graph.edges) {
    const list = outgoing.get(edge.source) ?? [];
    list.push({ edgeId: edge.id, target: edge.target });
    outgoing.set(edge.source, list);
  }

  const queue: string[] = [rootId];
  visitedNodes.add(rootId);
  while (queue.length > 0) {
    const current = queue.shift()!;
    for (const next of outgoing.get(current) ?? []) {
      visitedEdges.add(next.edgeId);
      if (!visitedNodes.has(next.target)) {
        visitedNodes.add(next.target);
        queue.push(next.target);
      }
    }
  }
  return { nodes: visitedNodes, edges: visitedEdges };
}

function layoutLayeredGraph(graph: DependencyGraph): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'LR', ranksep: 90, nodesep: 28, marginx: 24, marginy: 24 });

  for (const node of graph.nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of graph.edges) {
    g.setEdge(edge.source, edge.target);
  }
  dagre.layout(g);

  // Enforce deterministic 4-column horizontal alignment while preserving dagre vertical ordering
  const positions = new Map<string, { x: number; y: number }>();
  const byColumn: Record<number, GraphNodeData[]> = { 0: [], 1: [], 2: [], 3: [] };
  for (const node of graph.nodes) {
    byColumn[node.column]?.push(node);
  }

  for (const col of [0, 1, 2, 3]) {
    const colNodes = byColumn[col];
    colNodes.sort((a, b) => {
      const ay = g.node(a.id)?.y ?? 0;
      const by = g.node(b.id)?.y ?? 0;
      return ay - by || a.id.localeCompare(b.id);
    });
    colNodes.forEach((node, idx) => {
      const dagrePos = g.node(node.id);
      const y = dagrePos ? dagrePos.y - NODE_HEIGHT / 2 : idx * (NODE_HEIGHT + 28);
      positions.set(node.id, { x: 24 + col * COLUMN_X_STEP, y });
    });
  }

  // Prevent any accidental vertical overlap inside a column
  for (const col of [0, 1, 2, 3]) {
    const colNodes = byColumn[col];
    let lastBottom = -Infinity;
    for (const node of colNodes) {
      const pos = positions.get(node.id)!;
      const minY = lastBottom + 20;
      if (pos.y < minY) {
        pos.y = minY;
      }
      lastBottom = pos.y + NODE_HEIGHT;
    }
  }

  return positions;
}

export function BlastRadiusGraph({
  graph,
  blastRootId,
  onSelectBlastRoot,
  onInspectNode,
  onlyStaleAndConflicts,
}: BlastRadiusGraphProps) {
  const filteredGraph = useMemo<DependencyGraph>(() => {
    if (!onlyStaleAndConflicts) return graph;
    const staleNodes = new Set(
      graph.nodes
        .filter((n) => n.status.toUpperCase() !== 'VERIFIED' && n.status.toUpperCase() !== 'PASS')
        .map((n) => n.id),
    );
    return {
      nodes: graph.nodes.filter((n) => staleNodes.has(n.id)),
      edges: graph.edges.filter((e) => staleNodes.has(e.source) && staleNodes.has(e.target)),
    };
  }, [graph, onlyStaleAndConflicts]);

  const positions = useMemo(() => layoutLayeredGraph(filteredGraph), [filteredGraph]);

  const blast = useMemo(
    () => computeBlastRadius(filteredGraph, blastRootId),
    [filteredGraph, blastRootId],
  );

  const flowNodes = useMemo<CairnmindFlowNode[]>(() => {
    const hasBlast = Boolean(blastRootId && blast.nodes.size > 0);
    return filteredGraph.nodes.map((node) => ({
      id: node.id,
      type: 'glassNode',
      position: positions.get(node.id) ?? { x: node.column * COLUMN_X_STEP, y: 0 },
      draggable: false,
      data: {
        node,
        dimmed: hasBlast && !blast.nodes.has(node.id),
        highlighted: hasBlast && blast.nodes.has(node.id),
        selectedFocus: node.id === blastRootId,
      },
    }));
  }, [blast.nodes, blastRootId, filteredGraph.nodes, positions]);

  const flowEdges = useMemo<CairnmindFlowEdge[]>(() => {
    const hasBlast = Boolean(blastRootId && blast.nodes.size > 0);
    return filteredGraph.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: 'statusEdge',
      data: {
        status: edge.status,
        illuminated: hasBlast && blast.edges.has(edge.id),
        dimmed: hasBlast && !blast.edges.has(edge.id),
      },
    }));
  }, [blast.edges, blast.nodes.size, blastRootId, filteredGraph.edges]);

  const handleNodeClick: NodeMouseHandler<CairnmindFlowNode> = useCallback(
    (_, flowNode) => {
      const raw = flowNode.data.node;
      onInspectNode(raw);
      if (raw.category === 'source_file' || raw.category === 'evidence' || raw.category === 'claim') {
        onSelectBlastRoot(blastRootId === raw.id ? null : raw.id);
      } else {
        onSelectBlastRoot(null);
      }
    },
    [blastRootId, onInspectNode, onSelectBlastRoot],
  );

  const affectedCount = blastRootId ? Math.max(0, blast.nodes.size - 1) : 0;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* Header bar with 4-column taxonomy legend & Blast Radius indicator */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.06] px-3 py-2">
        <div className="flex items-center gap-2">
          <GitFork className="h-4 w-4 text-sky-400" strokeWidth={1.5} />
          <span className="text-xs font-semibold text-zinc-100">
            Dependency &amp; Blast-Radius Graph
          </span>
          <div className="hidden items-center gap-1.5 text-[10px] text-zinc-500 font-mono-tabular xl:flex">
            {COLUMN_LABELS.map((label, idx) => (
              <span key={label} className="flex items-center gap-1.5">
                {idx > 0 && <span className="text-zinc-700">-&gt;</span>}
                <span className="rounded border border-white/[0.06] bg-white/[0.02] px-1.5 py-0.5 text-zinc-400">
                  {label}
                </span>
              </span>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {blastRootId ? (
            <>
              <GlassBadge tone="amber">
                <Crosshair className="h-3 w-3" strokeWidth={1.5} />
                <span>Blast Radius: {affectedCount} downstream</span>
              </GlassBadge>
              <button
                type="button"
                onClick={() => onSelectBlastRoot(null)}
                className="flex h-6 items-center gap-1 rounded-md border border-white/[0.1] bg-white/[0.04] px-2 text-[11px] text-zinc-300 transition-colors hover:bg-white/[0.08]"
              >
                <RotateCcw className="h-3 w-3" strokeWidth={1.5} />
                <span>Reset</span>
              </button>
            </>
          ) : (
            <span className="text-[11px] text-zinc-500 font-mono-tabular">
              Click any Source File or Evidence node to illuminate downstream blast radius
            </span>
          )}
        </div>
      </div>

      {/* Interactive React Flow Canvas */}
      <div className="relative min-h-0 flex-1">
        {flowNodes.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-zinc-500 font-mono-tabular">
            No dependency nodes match the active filter.
          </div>
        ) : (
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodeClick={handleNodeClick}
            onPaneClick={() => onSelectBlastRoot(null)}
            fitView
            fitViewOptions={{ padding: 0.16, maxZoom: 1.05 }}
            minZoom={0.35}
            maxZoom={1.6}
            proOptions={{ hideAttribution: true }}
          >
            <Background
              variant={BackgroundVariant.Dots}
              gap={20}
              size={1}
              color="rgba(255, 255, 255, 0.08)"
            />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>
    </div>
  );
}
