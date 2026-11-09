import { describe, expect, it } from 'vitest';
import { ackLabel } from '../src/run_state';
import type { ClientRun } from '../src/types';
const run: ClientRun = { id: 'run-a', generation: 2, client: 'codex', session_id: 'session-a' };
describe('native memory receipt badge', () => {
  it('does not claim ACK from terminal attachment or a ready packet', () => {
    expect(ackLabel({ ...run, memory: { status: 'ready', acknowledged: false } }).text).toBe(
      'ACK wymagany',
    );
  });
  it('shows confirmed receipt only when the server reports acknowledged=true', () => {
    expect(ackLabel({ ...run, memory: { status: 'ready', acknowledged: true } })).toEqual({
      text: 'ACK potwierdzony',
      tone: 'good',
    });
  });
  it('keeps partial packets visibly blocked', () => {
    expect(
      ackLabel({ ...run, memory: { status: 'partial', unknowns: ['archive incomplete'] } }).text,
    ).toBe('Pakiet partial');
  });
});
