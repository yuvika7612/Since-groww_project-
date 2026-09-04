import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { formatTime } from '../components/FreshnessBadge';

export function SymbolDetail() {
  const { id = '' } = useParams();
  const { data, isLoading } = useQuery({
    queryKey: ['symbol', id],
    queryFn: () => api.symbol(id),
    enabled: Boolean(id),
  });

  return (
    <div className="shell">
      <nav style={{ marginBottom: '1.5rem' }}>
        <Link to="/" className="small">
          &larr; Digest
        </Link>
      </nav>

      {isLoading && <p className="muted">Loading...</p>}

      {data && (
        <>
          <h1 className="serif" style={{ fontSize: '1.5rem', fontWeight: 500, margin: 0 }}>
            {data.name}
          </h1>
          <p className="muted small" style={{ margin: '0.25rem 0 0' }}>
            {data.symbol} &middot; {data.exchange}
            {data.sector ? ` · ${data.sector}` : ''}
          </p>

          {data.stats && (
            <section style={{ marginTop: '1.75rem' }}>
              <h2 className="muted small" style={{ fontWeight: 600, margin: '0 0 0.5rem' }}>
                What normal looks like for this symbol
              </h2>
              <dl style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '0.375rem 1rem', margin: 0 }}>
                <dt className="muted small">Typical daily move</dt>
                <dd className="tabular" style={{ margin: 0 }}>
                  &plusmn;{(data.stats.expected_daily_move * 100).toFixed(2)}%
                </dd>
                <dt className="muted small">Beta vs index</dt>
                <dd className="tabular" style={{ margin: 0 }}>
                  {data.stats.beta_60d.toFixed(2)}
                </dd>
                <dt className="muted small">52-week range</dt>
                <dd className="tabular" style={{ margin: 0 }}>
                  {data.stats.low_52w.toFixed(2)} &ndash; {data.stats.high_52w.toFixed(2)}
                </dd>
                <dt className="muted small">Observations</dt>
                <dd className="tabular" style={{ margin: 0 }}>
                  {data.stats.sample_size}
                  {data.stats.sample_size < 30 && (
                    <span className="muted small"> (too few to trust beta)</span>
                  )}
                </dd>
              </dl>
            </section>
          )}

          <section style={{ marginTop: '1.75rem' }}>
            <h2 className="muted small" style={{ fontWeight: 600, margin: '0 0 0.5rem' }}>
              Recent events
            </h2>
            {data.recent_events.length === 0 && <p className="muted">Nothing recorded.</p>}
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {data.recent_events.map((event, index) => (
                <li key={index} style={{ padding: '0.625rem 0', borderTop: '1px solid var(--rule)' }}>
                  <p style={{ margin: 0 }}>{event.explanation}</p>
                  <p className="muted small" style={{ margin: '0.1875rem 0 0' }}>
                    {formatTime(event.occurred_at)}
                  </p>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
