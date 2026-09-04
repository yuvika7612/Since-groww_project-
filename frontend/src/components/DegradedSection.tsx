/** Rows we could not price properly.
 *
 * Kept visually separate from market events on purpose: a broken feed is our
 * failure, not the market's, and mixing the two teaches the user to distrust
 * both. The caution colour is used here and nowhere else.
 */
import type { DigestRow } from '../api/client';
import { formatTime } from './FreshnessBadge';

export function DegradedSection({ rows }: { rows: DigestRow[] }) {
  if (rows.length === 0) return null;

  return (
    <section
      aria-label="Data issues"
      style={{ borderTop: '1px solid var(--rule)', paddingTop: '1.125rem' }}
    >
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {rows.map((row) => (
          <li
            key={row.symbol}
            style={{ display: 'flex', gap: '0.625rem', padding: '0.3125rem 0' }}
          >
            <span aria-hidden="true" style={{ color: 'var(--caution)' }}>
              &#9888;
            </span>
            <span className="small">
              <strong style={{ fontWeight: 600 }}>{row.symbol}</strong>{' '}
              <span className="muted">
                {row.data_note ??
                  (row.freshness === 'stale'
                    ? `feed last updated ${formatTime(row.as_of)}`
                    : 'no current price')}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
