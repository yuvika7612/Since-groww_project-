/** How honest the price is, stated on the row.
 *
 * live         nothing at all. Live is the default and silence is correct.
 * delayed      a quiet label. A delayed feed rendered as live is the single
 *              failure that permanently costs a market product its credibility.
 * stale        the price goes muted and carries the time it was true.
 * unavailable  an em dash. Never a last-known price dressed up as current.
 */
import type { Freshness } from '../api/client';

export function FreshnessBadge({ freshness, asOf }: { freshness: string; asOf?: string | null }) {
  if (freshness === 'live') return null;

  if (freshness === 'delayed') {
    return <span className="muted small">15m delayed</span>;
  }

  if (freshness === 'stale') {
    return <span className="muted small">as of {formatTime(asOf)}</span>;
  }

  return <span className="muted small">no current price</span>;
}

export function formatTime(value?: string | null): string {
  if (!value) return '--:--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '--:--';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** The price itself, rendered according to how much we trust it. */
export function Price({ value, freshness }: { value: number | null; freshness: string }) {
  // An em dash, and no number at all. The row still exists and still says
  // why; it simply does not pretend to a price it does not have.
  if (value === null || freshness === 'unavailable') {
    return (
      <span className="tabular muted" aria-label="No current price">
        &mdash;
      </span>
    );
  }
  return (
    <span className={`tabular${freshness === 'stale' ? ' muted' : ''}`}>
      {value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  );
}

export type { Freshness };
