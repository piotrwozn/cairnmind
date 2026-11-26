import { useEffect, useRef, useState } from 'react';
import { buildEventsUrl } from '@/api/client';
import type { UiEventType, UiSseEvent } from '@/types/contracts';

export type ConnectionState = 'LIVE' | 'RECONNECTING';

const EVENT_TYPES: UiEventType[] = [
  'state.updated',
  'session.ack',
  'evidence.stale',
  'mcp.write.accepted',
  'mcp.write.rejected',
];

const MAX_STREAM_EVENTS = 120;

export interface UseEventStreamResult {
  connection: ConnectionState;
  events: UiSseEvent[];
  clearEvents: () => void;
}

export function useUiEventStream(onEvent?: (event: UiSseEvent) => void): UseEventStreamResult {
  const [connection, setConnection] = useState<ConnectionState>('RECONNECTING');
  const [events, setEvents] = useState<UiSseEvent[]>([]);
  const callbackRef = useRef(onEvent);
  callbackRef.current = onEvent;

  useEffect(() => {
    const url = buildEventsUrl();
    const source = new EventSource(url);

    const handleOpen = () => {
      setConnection('LIVE');
    };

    const handleError = () => {
      setConnection('RECONNECTING');
    };

    const listeners: Array<[string, EventListener]> = [];

    for (const eventType of EVENT_TYPES) {
      const listener: EventListener = (rawEvent) => {
        const message = rawEvent as MessageEvent<string>;
        setConnection('LIVE');
        try {
          const parsed = JSON.parse(message.data) as UiSseEvent;
          const normalized: UiSseEvent = {
            id: parsed.id || message.lastEventId || `${eventType}-${Date.now()}`,
            seq: parsed.seq,
            type: parsed.type || eventType,
            timestamp: parsed.timestamp || new Date().toISOString(),
            payload: parsed.payload || {},
          };
          setEvents((prev) => [normalized, ...prev].slice(0, MAX_STREAM_EVENTS));
          callbackRef.current?.(normalized);
        } catch {
          // ignore malformed frames
        }
      };
      source.addEventListener(eventType, listener);
      listeners.push([eventType, listener]);
    }

    source.addEventListener('open', handleOpen);
    source.addEventListener('error', handleError);

    return () => {
      source.removeEventListener('open', handleOpen);
      source.removeEventListener('error', handleError);
      for (const [type, fn] of listeners) {
        source.removeEventListener(type, fn);
      }
      source.close();
    };
  }, []);

  return {
    connection,
    events,
    clearEvents: () => setEvents([]),
  };
}
