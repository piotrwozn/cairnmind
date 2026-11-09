import { useEffect, useState } from 'react';
import { ArrowLeft, ChevronRight, FileQuestion } from 'lucide-react';
import { api, message, query } from './api';
import { Empty, label, object } from './components';
import type { Json } from './types';
export function InboxBrowser({ initialId }: { initialId?: string }) {
  const [items, setItems] = useState<Json[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [selected, setSelected] = useState(initialId ?? ''),
    [view, setView] = useState('raw'),
    [text, setText] = useState(''),
    [metadata, setMetadata] = useState<Json>({}),
    [next, setNext] = useState<number | null>(null),
    [error, setError] = useState('');
  const load = async (after?: string) => {
    try {
      const value = await api<{ items: Json[]; next_cursor?: string }>(
        '/api/inspector/inbox' + query({ cursor: after, limit: 100 }),
      );
      setItems((old) => (after ? [...old, ...value.items] : value.items));
      setCursor(value.next_cursor ?? null);
    } catch (err) {
      setError(message(err));
    }
  };
  const source = async (id: string, mode = view, offset = 0) => {
    try {
      const value = await api<Json>(
        '/api/inspector/inbox/' +
          encodeURIComponent(id) +
          query({ view: mode, offset, limit: 65536 }),
      );
      setSelected(id);
      setView(mode);
      setText((old) => (offset ? old + label(value.text) : label(value.text)));
      setMetadata(object(value.source ?? value.item));
      setNext(typeof value.next_offset === 'number' ? value.next_offset : null);
    } catch (err) {
      setError(message(err));
    }
  };
  useEffect(() => {
    void load();
    if (initialId) void source(initialId);
  }, []);
  return (
    <div>
      <p className="hint">
        Te wpisy nie są przypisane do projektu. Pozostają poza pamięcią projektów do czasu
        jednoznacznego przypisania.
      </p>
      {error && (
        <p className="form-error" role="status">
          {error}
        </p>
      )}
      {selected ? (
        <div className="source-detail">
          <button className="back-button" onClick={() => setSelected('')}>
            <ArrowLeft size={13} />
            Wszystkie nieprzypisane
          </button>
          <p className="path">{label(metadata.uri) || label(metadata.id) || selected}</p>
          <div className="segmented">
            <button
              className={view === 'raw' ? 'active' : ''}
              onClick={() => void source(selected, 'raw')}
            >
              Surowy zapis
            </button>
            <button
              className={view === 'metadata' ? 'active' : ''}
              onClick={() => void source(selected, 'metadata')}
            >
              Metadane
            </button>
          </div>
          <pre className="source-text">{text || '(Pusty wpis)'}</pre>
          {next !== null && (
            <button className="button" onClick={() => void source(selected, view, next)}>
              Dalszy fragment
            </button>
          )}
        </div>
      ) : (
        <div className="record-list">
          {items.map((item) => (
            <button
              className="record-row"
              key={label(item.id)}
              onClick={() => void source(label(item.id))}
            >
              <span className="record-kind">
                <FileQuestion size={15} />
              </span>
              <span className="record-row-content">
                <span>{label(item.uri) || label(item.id)}</span>
                <small>{label(item.kind) || 'Nieprzypisany wpis'}</small>
              </span>
              <ChevronRight size={13} />
            </button>
          ))}
          {!items.length && <Empty title="Brak nieprzypisanych wpisów" />}
          {cursor && (
            <button className="load-more" onClick={() => void load(cursor)}>
              Wczytaj więcej
            </button>
          )}
        </div>
      )}
    </div>
  );
}
