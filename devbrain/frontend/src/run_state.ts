import { object } from './components';
import type { ClientRun } from './types';
export function ackLabel(run?: ClientRun): { text: string; tone: string } {
  const readiness = object(run?.memory_readiness ?? run?.memory);
  if (readiness.acknowledged === true) return { text: 'ACK potwierdzony', tone: 'good' };
  if (readiness.status === 'partial' || readiness.status === 'unavailable')
    return { text: 'Pakiet ' + String(readiness.status), tone: 'warn' };
  if (run?.session_id || readiness.status) return { text: 'ACK wymagany', tone: 'warn' };
  return { text: 'Pamięć: oczekiwanie', tone: 'muted' };
}
