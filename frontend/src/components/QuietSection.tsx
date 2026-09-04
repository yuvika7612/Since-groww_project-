/** The rows that did nothing, counted and collapsed.
 *
 * Reporting that nothing happened is a real answer, and it is delivered as
 * one line rather than eleven grey rows competing for the same attention the
 * ranking just finished protecting. Expandable, because "show me anyway" is
 * a reasonable thing to want.
 */
import { useState } from 'react';
import type { DigestRow } from '../api/client';
import { Price } from './FreshnessBadge';
import { Change } from './AttentionRow';

export function QuietSection({ rows, summary }: { rows: DigestRow[]; summary: string }) {
  const [open, setOpen] = useState(false);
  if (rows.length === 0) return null;

  return (
    <section style={{ borderTop: '1px solid var(--rule)', paddingTop: '1.125rem' }}>
      <button
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="muted"
        style={{ display: 'flex', width: '100%', alignItems: 'center', gap: '0.5rem' }}
      >
        <span>{summary || `${rows.length} others: nothing meaningful.`}</span>
        <span aria-hidden="true" style={{ marginLeft: 'auto' }}>
          {open ? '⌃' : '⌄'}
        </span>
      </button>

      {open && (
        <ul style={{ listStyle: 'none', margin: '0.875rem 0 0', padding: 0 }}>
          {rows.map((row) => (
            <li
              key={row.symbol}
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: '0.75rem',
                padding: '0.4375rem 0',
              }}
            >
              <span>{row.symbol}</span>
              <span
                style={{
                  marginLeft: 'auto',
                  display: 'flex',
                  gap: '0.75rem',
                  alignItems: 'baseline',
                }}
              >
                <Price value={row.price ?? null} freshness={row.freshness} />
                <Change value={row.change_since_seen} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
