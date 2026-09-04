/** One row that earned a slot in the attention budget.
 *
 * The reason is always visible, never behind a tap. A row the user has to
 * interrogate to understand has not actually told them anything, and the
 * backend went to real trouble to produce a sentence that stands on its own.
 */
import { useNavigate } from 'react-router-dom';
import type { DigestRow } from '../api/client';
import { FreshnessBadge, Price } from './FreshnessBadge';

interface Props {
  row: DigestRow;
  trackRef?: (element: HTMLElement | null) => void;
  unread?: boolean;
}

export function Change({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) {
    return <span className="tabular muted">&mdash;</span>;
  }
  const up = value >= 0;
  const percent = `${up ? '+' : ''}${(value * 100).toFixed(2)}%`;
  return (
    <span className={`tabular ${up ? 'up' : 'down'}`}>
      {/* Glyph as well as colour. Direction is never carried by colour
          alone -- that fails for colour-blind readers and in sunlight. */}
      <span aria-hidden="true">{up ? '▲' : '▼'}</span>{' '}
      {percent}
    </span>
  );
}

export function AttentionRow({ row, trackRef, unread }: Props) {
  const navigate = useNavigate();
  const label = `${row.name}, ${row.primary_reason ?? 'no reason recorded'}`;

  return (
    <article
      ref={trackRef}
      style={{ padding: '1.125rem 0', borderTop: '1px solid var(--rule)' }}
    >
      <button
        onClick={() => navigate(`/symbol/${row.symbol}`)}
        aria-label={label}
        style={{ display: 'block', width: '100%', textAlign: 'left' }}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.75rem' }}>
          {unread && (
            <span
              aria-label="Unread"
              title="Unread"
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: 'var(--unseen)',
                flex: '0 0 auto',
                transform: 'translateY(-2px)',
              }}
            />
          )}
          <span style={{ fontWeight: 600 }}>{row.symbol}</span>
          <span
            style={{
              marginLeft: 'auto',
              display: 'flex',
              alignItems: 'baseline',
              gap: '0.75rem',
            }}
          >
            <Price value={row.price ?? null} freshness={row.freshness} />
            <Change value={row.change_since_seen} />
          </span>
        </div>

        {row.primary_reason && (
          <p className="muted" style={{ margin: '0.3125rem 0 0', maxWidth: '54ch' }}>
            {row.primary_reason}
          </p>
        )}

        {(row.freshness !== 'live' || row.data_note) && (
          <p style={{ margin: '0.3125rem 0 0' }}>
            <FreshnessBadge freshness={row.freshness} asOf={row.as_of} />
          </p>
        )}
      </button>
    </article>
  );
}
