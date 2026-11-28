import { BaseEdge, getSmoothStepPath, type Edge, type EdgeProps } from '@xyflow/react';

export interface FlowEdgePayload extends Record<string, unknown> {
  status: string;
  illuminated: boolean;
  dimmed: boolean;
}

export type CairnmindFlowEdge = Edge<FlowEdgePayload, 'statusEdge'>;

function strokeColor(status: string, illuminated: boolean): string {
  if (!illuminated) return 'rgba(161, 161, 170, 0.28)';
  switch (status.toUpperCase()) {
    case 'REJECTED':
    case 'FAIL':
    case 'CONFLICT':
      return '#fb7185';
    case 'STALE':
    case 'STALE_CODE':
      return '#fbbf24';
    case 'VERIFIED':
    case 'PASS':
      return '#34d399';
    default:
      return '#38bdf8';
  }
}

export function AnimatedStatusEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<CairnmindFlowEdge>) {
  const [edgePath] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 8,
  });

  const illuminated = Boolean(data?.illuminated);
  const dimmed = Boolean(data?.dimmed);
  const stroke = strokeColor(data?.status ?? 'ACTIVE', illuminated);

  return (
    <BaseEdge
      id={id}
      path={edgePath}
      className={illuminated ? 'cm-edge-animated' : undefined}
      style={{
        stroke,
        strokeWidth: illuminated ? 2 : 1.25,
        opacity: dimmed ? 0.16 : illuminated ? 1 : 0.75,
        transition: 'opacity 150ms ease, stroke 150ms ease',
      }}
    />
  );
}
