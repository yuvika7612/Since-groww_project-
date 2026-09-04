/** Add, remove, cost basis and note. */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { digestKey } from '../hooks/useDigest';
import { SymbolSearch } from './SymbolSearch';
import { Price } from './FreshnessBadge';

export function WatchlistManager() {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const { data: lists } = useQuery({ queryKey: ['watchlists'], queryFn: () => api.watchlists() });
  const watchlistId = lists?.[0]?.id;

  const { data: detail } = useQuery({
    queryKey: ['watchlist', watchlistId],
    queryFn: () => api.watchlist(watchlistId as number),
    enabled: Boolean(watchlistId),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['watchlist', watchlistId] });
    void queryClient.invalidateQueries({ queryKey: ['watchlists'] });
    void queryClient.invalidateQueries({ queryKey: digestKey });
  };

  const add = useMutation({
    mutationFn: (symbol: string) => api.addItem(watchlistId as number, { symbol }),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError: (e: Error) => setError(e.message.includes('409') ? 'Already on your list.' : e.message),
  });

  const remove = useMutation({
    mutationFn: (symbol: string) => api.removeItem(watchlistId as number, symbol),
    onSuccess: refresh,
  });

  const update = useMutation({
    mutationFn: ({ symbol, cost_basis, note }: { symbol: string; cost_basis?: number | null; note?: string | null }) =>
      api.updateItem(watchlistId as number, symbol, { cost_basis, note }),
    onSuccess: refresh,
  });

  if (!watchlistId) return <p className="muted">No watchlist yet.</p>;

  return (
    <div>
      <SymbolSearch onPick={(symbol) => add.mutate(symbol)} />
      {error && (
        <p className="small" style={{ color: 'var(--caution)', margin: '0.5rem 0 0' }}>
          {error}
        </p>
      )}

      <ul style={{ listStyle: 'none', margin: '1.5rem 0 0', padding: 0 }}>
        {detail?.items.map((item) => (
          <li key={item.symbol} style={{ padding: '0.875rem 0', borderTop: '1px solid var(--rule)' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.75rem' }}>
              <span style={{ fontWeight: 600 }}>{item.symbol}</span>
              <span className="muted small">{item.name}</span>
              <span style={{ marginLeft: 'auto', display: 'flex', gap: '0.75rem', alignItems: 'baseline' }}>
                <Price value={item.price ?? null} freshness={item.freshness ?? 'unavailable'} />
                <button
                  onClick={() => remove.mutate(item.symbol)}
                  className="small muted"
                  aria-label={`Remove ${item.symbol}`}
                >
                  Remove
                </button>
              </span>
            </div>

            <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.5rem', flexWrap: 'wrap' }}>
              <label className="muted small">
                Cost basis{' '}
                <input
                  type="number"
                  step="0.01"
                  defaultValue={item.cost_basis ?? ''}
                  onBlur={(event) =>
                    update.mutate({
                      symbol: item.symbol,
                      cost_basis: event.target.value === '' ? null : Number(event.target.value),
                    })
                  }
                  className="tabular"
                  style={{ width: '8rem' }}
                />
              </label>
              <label className="muted small" style={{ flex: 1, minWidth: '12rem' }}>
                Note{' '}
                <input
                  defaultValue={item.note ?? ''}
                  onBlur={(event) => update.mutate({ symbol: item.symbol, note: event.target.value || null })}
                  style={{ width: '100%' }}
                />
              </label>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
