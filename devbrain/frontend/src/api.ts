let csrf = '';
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export function query(
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const values = new URLSearchParams();
  for (const [key, value] of Object.entries(params))
    if (value !== undefined && value !== null && value !== '') values.set(key, String(value));
  const encoded = values.toString();
  return encoded ? '?' + encoded : '';
}
export async function api<T = Record<string, unknown>>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && !['GET', 'HEAD'].includes(options.method))
    headers.set('X-CSRF-Token', csrf);
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
  });
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    const detail = (data as { detail?: unknown })?.detail;
    throw new ApiError(
      typeof detail === 'string' ? detail : `Operacja nie powiodła się (${response.status}).`,
      response.status,
    );
  }
  return data as T;
}
export function post<T = Record<string, unknown>>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) });
}
export function put<T = Record<string, unknown>>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'PUT', body: JSON.stringify(body) });
}
let bootstrapPromise: Promise<void> | null = null;
export function bootstrap(): Promise<void> {
  return (bootstrapPromise ??= bootstrapSession());
}
async function bootstrapSession(): Promise<void> {
  const fragment = window.location.hash.slice(1);
  const ticket = new URLSearchParams(fragment).get('ticket');
  // Remove the one-use capability before network calls, errors or resource loads.
  if (fragment) history.replaceState(null, '', window.location.pathname + window.location.search);
  const session = ticket
    ? await post<{ csrf?: string; csrf_token?: string }>('/auth/exchange', { ticket })
    : await api<{ csrf?: string; csrf_token?: string }>('/auth/session');
  csrf = session.csrf ?? session.csrf_token ?? '';
  if (!csrf)
    throw new ApiError('Brak aktywnej sesji kokpitu. Otwórz go ponownie poleceniem devbrain.', 401);
}
export function terminalUrl(id: string, generation: number): string {
  const url = new URL(
    `/api/client-runs/${encodeURIComponent(id)}/terminal`,
    window.location.origin,
  );
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  url.search = query({ csrf, generation });
  return url.toString();
}
export function encodeInput(text: string): string {
  return btoa(Array.from(new TextEncoder().encode(text), (x) => String.fromCharCode(x)).join(''));
}
export function decodeOutput(data: string): Uint8Array {
  return Uint8Array.from(atob(data), (x) => x.charCodeAt(0));
}
export const message = (error: unknown): string =>
  error instanceof Error ? error.message : 'Nieoczekiwany błąd.';
