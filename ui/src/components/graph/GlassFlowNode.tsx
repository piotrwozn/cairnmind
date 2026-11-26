import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { BookOpen, FileCode2, FlaskConical, Layers, Quote } from 'lucide-react';
import { GlassBadge, statusToTone } from '@/components/glass/GlassBadge';
import { cn } from '@/lib/utils';
import type { GraphNodeData } from '@/types/contracts';

export interface FlowNodePayload extends Record<string, unknown> {
  node: GraphNodeData;
  dimmed: boolean;
  highlighted: boolean;
  selectedFocus: boolean;
}

export type CairnmindFlowNode = Node<FlowNodePayload, 'glassNode'>;

function nodeIcon(node: GraphNodeData) {
  if (node.category === 'source_file') return FileCode2;
  if (node.category === 'evidence') {
    return node.meta.evidence_type === 'test_run' ? FlaskConical : Quote;
  }
  if (node.category === 'claim') return BookOpen;
  return Layers;
}

function accentBorder(status: string, selectedFocus: boolean, highlighted: boolean): string {
  if (selectedFocus) return 'border-sky-400/70 bg-sky-500/[0.12]';
  switch (status.toUpperCase()) {
    case 'REJECTED':
    case 'CONFLICT':
    case 'FAIL':
      return highlighted
        ? 'border-rose-400/70 bg-rose-500/[0.12]'
        : 'border-rose-500/35 bg-zinc-950/75';
    case 'STALE':
    case 'STALE_CODE':
      return highlighted
        ? 'border-amber-400/70 bg-amber-500/[0.12]'
        : 'border-amber-500/35 bg-zinc-950/75';
    case 'VERIFIED':
    case 'PASS':
      return highlighted
        ? 'border-emerald-400/60 bg-emerald-500/[0.10]'
        : 'border-emerald-500/25 bg-zinc-950/75';
    default:
      return 'border-white/[0.10] bg-zinc-950/75';
  }
}

export function GlassFlowNode({ data }: NodeProps<CairnmindFlowNode>) {
  const { node, dimmed, highlighted, selectedFocus } = data;
  const Icon = nodeIcon(node);
  const tone = statusToTone(node.status);

  return (
    <div
      className={cn(
        'w-[228px] rounded-md border px-2.5 py-2 backdrop-blur-xl transition-all duration-150 shadow-[0_8px_24px_rgba(0,0,0,0.45),inset_0_1px_0_0_rgba(255,255,255,0.06)]',
        accentBorder(node.status, selectedFocus, highlighted),
        dimmed ? 'opacity-25' : 'opacity-100',
      )}
    >
      {node.column > 0 && (
        <Handle
          type="target"
          position={Position.Left}
          className="!h-2 !w-2 !border-white/20 !bg-zinc-800"
        />
      )}

      <div className="flex items-center justify-between gap-1.5">
        <div className="flex min-w-0 items-center gap-1.5">
          <Icon className="h-3.5 w-3.5 shrink-0 text-zinc-300" strokeWidth={1.5} />
          <span className="truncate text-[11px] font-semibold text-zinc-100 font-mono-tabular">
            {node.label}
          </span>
        </div>
        <GlassBadge tone={tone} className="shrink-0 text-[10px]">
          {node.status}
        </GlassBadge>
      </div>

      <div
        className="mt-1 truncate text-[10px] text-zinc-400 font-mono-tabular"
        title={node.subtitle}
      >
        {node.subtitle}
      </div>

      {node.column < 3 && (
        <Handle
          type="source"
          position={Position.Right}
          className="!h-2 !w-2 !border-white/20 !bg-zinc-800"
        />
      )}
    </div>
  );
}
