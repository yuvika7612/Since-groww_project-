import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

export const digestKey = ['digest'] as const;

export function useDigest(enabled = true) {
  return useQuery({
    queryKey: digestKey,
    queryFn: () => api.digest(),
    enabled,
    // Coming back to the tab is exactly when "what changed since I last
    // looked" needs recomputing.
    refetchOnWindowFocus: true,
    staleTime: 5_000,
    // A polling floor beneath the stream. SSE is a nudge, not a guarantee:
    // it only fires when an event is detected, so on a genuinely quiet
    // session nothing arrives and any snapshot taken at a bad moment -- a
    // cache that was still filling, a dropped connection -- would stay on
    // screen indefinitely. Ten seconds costs one cheap query and removes a
    // whole class of "the page is wrong and nothing will fix it".
    refetchInterval: 10_000,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    refetchInterval: 15_000,
  });
}
