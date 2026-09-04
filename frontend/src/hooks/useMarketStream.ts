/** SSE with reconnect.
 *
 * Reconnection is the whole reason SSE was chosen over WebSockets: it is in
 * the protocol rather than something to hand-roll. What the browser does not
 * do for us is decide what a gap means, and a gap means our local state is
 * untrustworthy -- so every reconnect refetches the digest rather than
 * assuming the deltas we missed did not matter.
 */
import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { auth } from '../api/client';
import { digestKey } from './useDigest';

const MAX_BACKOFF_MS = 30_000;
const BASE_BACKOFF_MS = 1_000;

export type StreamState = 'connected' | 'connecting' | 'offline';

export function useMarketStream(symbols: string[], marketState: string | undefined) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<StreamState>('offline');
  const attempt = useRef(0);
  const source = useRef<EventSource | null>(null);
  const retryTimer = useRef<number | null>(null);

  // A socket held open all night for data that cannot change is waste.
  const shouldStream =
    symbols.length > 0 && marketState !== 'closed' && marketState !== 'holiday';

  const key = symbols.join(',');

  useEffect(() => {
    if (!shouldStream) {
      source.current?.close();
      source.current = null;
      setStatus('offline');
      return;
    }

    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      setStatus('connecting');

      const token = auth.get();
      const url = `/api/stream?symbols=${encodeURIComponent(key)}${
        token ? `&token=${encodeURIComponent(token)}` : ''
      }`;
      const es = new EventSource(url);
      source.current = es;

      es.onopen = () => {
        if (cancelled) return;
        const reconnected = attempt.current > 0;
        attempt.current = 0;
        setStatus('connected');
        // Deltas were missed while we were away; local state is stale.
        if (reconnected) void queryClient.invalidateQueries({ queryKey: digestKey });
      };

      es.addEventListener('event', () => {
        void queryClient.invalidateQueries({ queryKey: digestKey });
      });

      es.addEventListener('quote', () => {
        // Prices move continuously and the digest is cheap; a refetch keeps
        // one source of truth rather than patching the cache by hand and
        // risking a row that disagrees with its own reason.
        void queryClient.invalidateQueries({ queryKey: digestKey });
      });

      es.addEventListener('market', () => {
        void queryClient.invalidateQueries({ queryKey: digestKey });
      });

      es.onerror = () => {
        es.close();
        source.current = null;
        if (cancelled) return;
        setStatus('offline');

        // Exponential backoff with jitter, capped. Jitter matters: without it
        // every client that dropped on the same restart reconnects in the
        // same millisecond.
        const backoff = Math.min(BASE_BACKOFF_MS * 2 ** attempt.current, MAX_BACKOFF_MS);
        const jittered = backoff * (0.5 + Math.random() * 0.5);
        attempt.current += 1;
        retryTimer.current = window.setTimeout(connect, jittered);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
      source.current?.close();
      source.current = null;
    };
  }, [key, shouldStream, queryClient]);

  return status;
}
