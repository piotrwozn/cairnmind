import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useUiEventStream, type ConnectionState } from '@/api/events';
import type { UiSseEvent } from '@/types/contracts';

export interface LiveInvalidationState {
  connection: ConnectionState;
  events: UiSseEvent[];
  clearEvents: () => void;
  stalePulseIds: ReadonlySet<string>;
  rejectedPulseIds: ReadonlySet<string>;
}

/**
 * Bridges SSE events into TanStack Query cache invalidation and 600ms semantic flash states.
 */
export function useLiveInvalidation(): LiveInvalidationState {
  const queryClient = useQueryClient();
  const [stalePulseIds, setStalePulseIds] = useState<Set<string>>(() => new Set());
  const [rejectedPulseIds, setRejectedPulseIds] = useState<Set<string>>(() => new Set());
  const timersRef = useRef<number[]>([]);

  useEffect(() => {
    return () => {
      for (const timer of timersRef.current) {
        window.clearTimeout(timer);
      }
    };
  }, []);

  const flashIds = useCallback((kind: 'stale' | 'rejected', ids: string[]) => {
    if (!ids.length) return;
    const setter = kind === 'stale' ? setStalePulseIds : setRejectedPulseIds;
    setter((prev) => {
      const next = new Set(prev);
      for (const id of ids) next.add(id);
      return next;
    });
    const timer = window.setTimeout(() => {
      setter((prev) => {
        const next = new Set(prev);
        for (const id of ids) next.delete(id);
        return next;
      });
    }, 650);
    timersRef.current.push(timer);
  }, []);

  const handleEvent = useCallback(
    (event: UiSseEvent) => {
      if (event.type === 'evidence.stale') {
        const claimIds = Array.isArray(event.payload.claim_ids)
          ? event.payload.claim_ids.filter((v): v is string => typeof v === 'string')
          : typeof event.payload.claim_id === 'string'
            ? [event.payload.claim_id]
            : [];
        flashIds('stale', claimIds);
      }

      if (event.type === 'mcp.write.rejected') {
        const auditId = typeof event.payload.audit_id === 'string' ? event.payload.audit_id : event.id;
        flashIds('rejected', [auditId]);
      }

      // Avoid refetch storm on the initial bootstrap frame
      if (event.payload.reason !== 'connected') {
        void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
      }
    },
    [flashIds, queryClient],
  );

  const { connection, events, clearEvents } = useUiEventStream(handleEvent);

  return {
    connection,
    events,
    clearEvents,
    stalePulseIds,
    rejectedPulseIds,
  };
}
