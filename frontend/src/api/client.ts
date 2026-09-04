/** Typed fetch wrapper.
 *
 * Auth is one bearer token in localStorage, matching the backend's dev-login.
 * Swapping in real OAuth touches this file and nothing else.
 */
import type { components } from './types';

export type Digest = components['schemas']['DigestOut'];
export type DigestRow = components['schemas']['DigestRowOut'];
export type MarketOut = components['schemas']['MarketOut'];
export type EventOut = components['schemas']['EventOut'];
export type WatchlistSummary = components['schemas']['WatchlistSummary'];
export type WatchlistDetail = components['schemas']['WatchlistDetail'];
export type WatchlistItemOut = components['schemas']['WatchlistItemOut'];
export type SymbolOut = components['schemas']['SymbolOut'];
export type SymbolDetail = components['schemas']['SymbolDetail'];
export type HealthOut = components['schemas']['HealthOut'];
export type ScenarioOut = components['schemas']['ScenarioOut'];
export type SeenResponse = components['schemas']['SeenResponse'];

export type Freshness = 'live' | 'delayed' | 'stale' | 'unavailable';

const TOKEN_KEY = 'since.token';

export const auth = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  // Declared as a field rather than a constructor parameter property: the
  // Vite template enables erasableSyntaxOnly, which forbids syntax that
  // cannot be stripped without emitting runtime code.
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = auth.get();
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });

  if (response.status === 401) {
    // The token is bad or gone. Clear it so the app falls back to login
    // rather than looping on failed requests.
    auth.clear();
    throw new ApiError(401, 'Sign in again');
  }
  if (!response.ok) {
    const body = await response.text();
    throw new ApiError(response.status, body || response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  devLogin: (email: string) =>
    request<{ user_id: number; token: string }>('/auth/dev-login', {
      method: 'POST',
      body: JSON.stringify({ email }),
    }),

  digest: (watchlistId?: number) =>
    request<Digest>(`/digest${watchlistId ? `?watchlist_id=${watchlistId}` : ''}`),

  markSeen: (entries: { symbol: string; seen_at: string; price: number | null }[]) =>
    request<SeenResponse>('/seen', {
      method: 'POST',
      body: JSON.stringify({ entries }),
    }),

  watchlists: () => request<WatchlistSummary[]>('/watchlists'),
  watchlist: (id: number) => request<WatchlistDetail>(`/watchlists/${id}`),

  addItem: (id: number, body: { symbol: string; cost_basis?: number | null; note?: string | null }) =>
    request<WatchlistItemOut>(`/watchlists/${id}/items`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updateItem: (id: number, symbol: string, body: { cost_basis?: number | null; note?: string | null }) =>
    request<WatchlistItemOut>(`/watchlists/${id}/items/${symbol}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  removeItem: (id: number, symbol: string) =>
    request<void>(`/watchlists/${id}/items/${symbol}`, { method: 'DELETE' }),

  searchSymbols: (q: string) =>
    request<SymbolOut[]>(`/symbols/search?q=${encodeURIComponent(q)}&limit=10`),

  symbol: (symbol: string) => request<SymbolDetail>(`/symbols/${symbol}`),

  health: () => fetch('/health').then((r) => r.json() as Promise<HealthOut>),

  scenarios: () => request<ScenarioOut[]>('/debug/scenarios'),
  runScenario: (key: string) =>
    request<{ scenario: string; title: string; now: string }>(`/debug/scenarios/${key}`, {
      method: 'POST',
    }),
};

/** Beacon flush for page-hide. A normal fetch is cancelled when a mobile tab
 *  is backgrounded, which is exactly when the write most needs to land. */
export function beaconSeen(
  entries: { symbol: string; seen_at: string; price: number | null }[],
): boolean {
  if (!navigator.sendBeacon || entries.length === 0) return false;
  const token = auth.get();
  if (!token) return false;
  // sendBeacon cannot set an Authorization header, so the token rides in the
  // query string on this one endpoint. Documented dev-auth limitation.
  const blob = new Blob([JSON.stringify({ entries })], { type: 'application/json' });
  return navigator.sendBeacon(`/api/seen?token=${encodeURIComponent(token)}`, blob);
}
