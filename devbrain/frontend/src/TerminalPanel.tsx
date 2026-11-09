import { useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { CircleStop, Keyboard, RotateCcw, Square, TerminalSquare, Unplug } from 'lucide-react';
import '@xterm/xterm/css/xterm.css';
import { decodeOutput, encodeInput, message, post, terminalUrl } from './api';
import { Badge, Empty, IconButton, label } from './components';
import type { ClientRun, Profile } from './types';

import { ackLabel } from './run_state';
export { ackLabel } from './run_state';
export function TerminalPanel({
  run,
  profile,
  onRefresh,
  onLogin,
  onResume,
  onNewSession,
  operational = false,
}: {
  run?: ClientRun;
  profile?: Profile;
  onRefresh: () => void;
  onLogin?: () => void;
  onResume?: () => void;
  onNewSession?: () => void;
  operational?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const [connection, setConnection] = useState('oczekiwanie');
  const [controller, setController] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!run || !host.current) return;
    let alive = true;
    const term = new Terminal({
      cursorBlink: true,
      convertEol: false,
      scrollback: 5000,
      fontSize: 13,
      lineHeight: 1.35,
      fontFamily: '"SFMono-Regular",Consolas,"Liberation Mono",monospace',
      theme: {
        background: '#0b0f12',
        foreground: '#d9e0e5',
        cursor: '#c6ec98',
        selectionBackground: '#34413c',
        black: '#172027',
        brightBlack: '#63727d',
        green: '#b5d78c',
        cyan: '#91c8d4',
        blue: '#a5b7e4',
        red: '#e8a49e',
        yellow: '#e7c48e',
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    terminal.current = term;
    // OSC 52 clipboard access is deliberately denied. Native selection/copy stays available.
    const denyClipboard = term.parser.registerOscHandler(52, () => true);
    const safeFit = () => {
      try {
        if (host.current?.clientWidth && host.current.clientHeight) fit.fit();
      } catch {
        /* Hidden panes resize when visible. */
      }
    };
    safeFit();
    const ws = new WebSocket(terminalUrl(run.id, run.generation));
    socket.current = ws;
    setConnection('łączenie');
    setController(false);
    setError('');
    ws.onopen = () => {
      if (!alive) return;
      setConnection('połączono');
      safeFit();
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    };
    ws.onmessage = (event) => {
      if (!alive) return;
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'output')
          term.write(
            data.encoding === 'base64' || typeof data.data === 'string'
              ? decodeOutput(data.data)
              : data.data,
          );
        else if (data.type === 'control') setController(data.controller === true);
        else if (data.type === 'gap') {
          term.write('\r\n\x1b[2m[Odtwarzanie aktualnego ekranu terminala]\x1b[0m\r\n');
        } else if (data.type === 'status') {
          onRefresh();
          if (data.run?.lifecycle === 'exited' || data.run?.state === 'exited')
            setConnection('zakończono');
        } else if (data.type === 'error')
          setError(label(data.message) || 'Błąd połączenia terminala.');
      } catch {
        setError('Nieprawidłowa wiadomość terminala.');
      }
    };
    ws.onclose = () => {
      if (alive) {
        setConnection('rozłączono');
        setController(false);
      }
    };
    ws.onerror = () => {
      if (alive) setError('Nie można połączyć terminala. Proces może nadal działać w tle.');
    };
    const input = term.onData((text) => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: 'input', data: encodeInput(text) }));
    });
    const resize = term.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'resize', cols, rows }));
    });
    const observer = new ResizeObserver(safeFit);
    observer.observe(host.current);
    return () => {
      alive = false;
      observer.disconnect();
      input.dispose();
      resize.dispose();
      denyClipboard.dispose();
      ws.close();
      socket.current = null;
      term.dispose();
      terminal.current = null;
    };
    // Run identity/generation controls attachment, not every state refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, run?.generation, attempt]);
  useEffect(() => {
    if (terminal.current) terminal.current.options.disableStdin = !controller;
  }, [controller]);
  const action = async (kind: string) => {
    if (!run) return;
    try {
      await post(`/api/client-runs/${encodeURIComponent(run.id)}/${kind}`, {
        generation: run.generation,
      });
      onRefresh();
    } catch (err) {
      setError(message(err));
    }
  };
  const readiness = ackLabel(run);
  const state = run?.lifecycle ?? run?.state;
  const inactive = ['exited', 'stopped', 'lost', 'failed', 'interrupted'].includes(state ?? '');
  return (
    <section
      className={'terminal-panel ' + (operational ? 'operational' : '')}
      aria-label={
        operational
          ? 'Terminal logowania'
          : `${profile?.label ?? run?.client ?? 'Agent'} — terminal`
      }
    >
      <div className="terminal-heading">
        <span className="terminal-client">
          <span className={'client-mark ' + (profile?.client ?? run?.client ?? '')}>⌘</span>
          <strong>{profile?.label ?? run?.client ?? 'Agent'}</strong>
          <span className="terminal-caption">
            {operational ? 'logowanie' : (state ?? 'gotowy do uruchomienia')}
            {!operational && run?.model ? ' · ' + String(run.model) : ''}
          </span>
        </span>
        <div className="terminal-actions">
          {!operational && <Badge tone={readiness.tone}>{readiness.text}</Badge>}
          {run && (
            <>
              <IconButton
                label="Przerwij bieżącą operację"
                onClick={() => void action('interrupt')}
              >
                <Square size={12} />
              </IconButton>
              <IconButton label="Zatrzymaj proces" onClick={() => void action('stop')}>
                <CircleStop size={15} />
              </IconButton>
            </>
          )}
        </div>
      </div>
      <div className="terminal-surface" ref={host} />
      {!run && (
        <div className="terminal-empty">
          <Empty title="Oryginalny interfejs CLI">
            <span>Sesja uruchomi się w katalogu projektu.</span>
          </Empty>
          {onLogin && (
            <button className="button subtle" onClick={onLogin}>
              <TerminalSquare size={14} />
              Stan połączenia / logowanie
            </button>
          )}
        </div>
      )}
      {error && (
        <div className="terminal-warning" role="status">
          {error}
        </div>
      )}
      {inactive && !operational && (
        <div className="terminal-recovery">
          <span>Proces zakończony. Pamięć projektu pozostaje zachowana.</span>
          {onResume && (
            <button className="button compact" onClick={onResume}>
              Wznów sesję
            </button>
          )}
          {onNewSession && (
            <button className="button compact" onClick={onNewSession}>
              Nowa sesja
            </button>
          )}
        </div>
      )}
      <div className="terminal-footer">
        <span>
          <span className={'status-dot ' + (connection === 'połączono' ? 'good' : '')} />
          {connection}
          {run && ' · ' + (controller ? 'sterowanie' : 'podgląd')}
        </span>
        <span className="terminal-footer-actions">
          {run && !controller && (
            <button
              onClick={() => {
                socket.current?.send(JSON.stringify({ type: 'takeover' }));
                terminal.current?.focus();
              }}
            >
              <Keyboard size={12} />
              Przejmij sterowanie
            </button>
          )}
          {run && connection === 'rozłączono' && (
            <button onClick={() => setAttempt((value) => value + 1)}>
              <RotateCcw size={12} />
              Połącz ponownie
            </button>
          )}
          {run && (
            <span title="Zamknięcie przeglądarki nie zatrzymuje procesu">
              <Unplug size={12} />
              odłączalne
            </span>
          )}
        </span>
      </div>
    </section>
  );
}
