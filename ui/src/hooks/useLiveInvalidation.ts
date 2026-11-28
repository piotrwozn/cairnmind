import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useUiEventStream, type ConnectionState } from '@/api/events';
import type { ReindexProgressState, UiSseEvent } from '@/types/contracts';

export interface LiveInvalidationState {
  connection: ConnectionState;
  events: UiSseEvent[];
  clearEvents: () => void;
  stalePulseIds: ReadonlySet<string>;
  rejectedPulseIds: ReadonlySet<string>;
  /** Live map of `file_path -> latest sha256` from SSE `repo_fingerprint_changed` events. */
  changedFingerprints: Readonly<Record<string, string>>;
  /** Live set of claim IDs reported stale during this session. */
  liveStaleClaimIds: ReadonlySet<string>;
  /** Live re-index progress bar state streamed over SSE `reindex.progress`. */
  reindexProgress: ReindexProgressState | null;
}

/**
 * Bridges SSE events into TanStack Query cache invalidation, live citation staleness,
 * re-index progress streaming, and 600ms semantic flash states.
 */
export function useLiveInvalidation(): LiveInvalidationState {
  const queryClient = useQueryClient();
  const [stalePulseIds, setStalePulseIds] = useState<Set<string>>(() => new Set());
  const [rejectedPulseIds, setRejectedPulseIds] = useState<Set<string>>(() => new Set());
  const [changedFingerprints, setChangedFingerprints] = useState<Record<string, string>>({});
  const [liveStaleClaimIds, setLiveStaleClaimIds] = useState<Set<string>>(() => new Set());
  const [reindexProgress, setReindexProgress] = useState<ReindexProgressState | null>(null);
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
        if (claimIds.length > 0) {
          setLiveStaleClaimIds((prev) => {
            const next = new Set(prev);
            for (const id of claimIds) next.add(id);
            return next;
          });
        }
      }

      if (event.type === 'repo_fingerprint_changed') {
        const filePath = typeof event.payload.file_path === 'string' ? event.payload.file_path : null;
        const newSha = typeof event.payload.new_sha256 === 'string' ? event.payload.new_sha256 : null;
        if (filePath && newSha) {
          setChangedFingerprints((prev) => ({ ...prev, [filePath]: newSha }));
        }
        const claimIds = Array.isArray(event.payload.claim_ids)
          ? event.payload.claim_ids.filter((v): v is string => typeof v === 'string')
          : [];
        if (claimIds.length > 0) {
          flashIds('stale', claimIds);
          setLiveStaleClaimIds((prev) => {
            const next = new Set(prev);
            for (const id of claimIds) next.add(id);
            return next;
          });
        }
      }

      if (event.type === 'reindex.progress') {
        const percent = typeof event.payload.percent === 'number' ? event.payload.percent : 0;
        const stage = typeof event.payload.stage === 'string' ? event.payload.stage : 'indexing';
        const message = typeof event.payload.message === 'string' ? event.payload.message : 'Re-indexing…';
        const jobId = typeof event.payload.job_id === 'string' ? event.payload.job_id : undefined;
        setReindexProgress({ active: percent < 100, job_id: jobId, percent, stage, message });
        if (percent >= 100) {
          const timer = window.setTimeout(() => {
            setReindexProgress((cur) => (cur && cur.job_id === jobId && cur.percent >= 100 ? null : cur));
          }, 2600);
          timersRef.current.push(timer);
        }
      }

      if (event.type === 'mcp.write.rejected') {
        const auditId = typeof event.payload.audit_id === 'string' ? event.payload.audit_id : event.id;
        flashIds('rejected', [auditId]);
      }

      // Avoid refetch storm on the initial bootstrap frame or intermediate progress frames
      if (event.payload.reason !== 'connected' && event.type !== 'reindex.progress') {
        void queryClient.invalidateQueries({ queryKey: ['ui-snapshot'] });
        void queryClient.invalidateQueries({ queryKey: ['ui-drift'] });
        void queryClient.invalidateQueries({ queryKey: ['ui-versions'] });
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
    changedFingerprints,
    liveStaleClaimIds,
    reindexProgress,
  };
}
