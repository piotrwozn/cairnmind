import { X, LoaderCircle } from 'lucide-react';
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import type { Json } from './types';
export function IconButton({
  label,
  children,
  onClick,
  disabled = false,
  className = '',
}: {
  label: string;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      className={'icon-button ' + className}
      title={label}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
export function Badge({ children, tone = 'muted' }: { children: ReactNode; tone?: string }) {
  return <span className={'badge ' + tone}>{children}</span>;
}
export function Busy({ children = 'Ładowanie' }: { children?: ReactNode }) {
  return (
    <span className="busy">
      <LoaderCircle size={14} />
      {children}
    </span>
  );
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-mark">◇</span>
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    el?.showModal();
    return () => el?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={wide ? 'modal wide' : 'modal'}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <IconButton label="Zamknij okno" onClick={onClose}>
          <X size={18} />
        </IconButton>
      </div>
      {children}
    </dialog>
  );
}
export function SmallData({ data }: { data: unknown }) {
  if (data === null || data === undefined) return null;
  return (
    <pre className="small-data">
      {typeof data === 'string' ? data : JSON.stringify(data, null, 2)}
    </pre>
  );
}
export function entries(value: unknown): Json[] {
  return Array.isArray(value) ? (value as Json[]) : [];
}
export function object(value: unknown): Json {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : {};
}
export function label(value: unknown): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
}
export function formatTime(value: unknown): string {
  if (!value) return '—';
  const time = new Date(String(value));
  return Number.isNaN(time.getTime())
    ? String(value)
    : time.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
}
