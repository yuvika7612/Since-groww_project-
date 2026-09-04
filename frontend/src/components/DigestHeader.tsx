/** The verdict. This is the hero of the page.
 *
 * On a quiet day the entire screen is one serif sentence saying nothing
 * happened. That is not an empty state waiting to be filled with an
 * illustration or a call to action -- it is the product working. Most
 * sessions, for most symbols, nothing happened, and a watchlist that cannot
 * say so plainly is why people check their phone eleven times a day.
 *
 * Newsreader rather than the UI face because the sentence is meant to be
 * read, not scanned. The serif is the reading-queue metaphor made visible.
 */
import type { ReactNode } from 'react';
import type { Digest } from '../api/client';

interface Props {
  digest: Digest | undefined;
  connection?: 'connected' | 'connecting' | 'offline';
  /** Rendered in the top-right beside the status dot, so the nav shares the
   *  app-name row instead of being overlapped into it. */
  action?: ReactNode;
}

/** "Since Tuesday, 10:12" -- the oldest watermark across the list, because
 *  the digest covers everything since the user's *earliest* unread row, not
 *  their most recent glance. */
function sinceLabel(digest: Digest | undefined): string {
  if (!digest) return '';
  const rows = [...digest.needs_attention, ...digest.quiet, ...digest.degraded];
  const stamps = rows
    .map((row) => row.seen_at)
    .filter((value): value is string => Boolean(value))
    .map((value) => new Date(value).getTime())
    .filter((value) => !Number.isNaN(value));

  if (stamps.length === 0) return 'Since the beginning';

  const oldest = new Date(Math.min(...stamps));
  const day = oldest.toLocaleDateString(undefined, { weekday: 'long' });
  const time = oldest.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  return `Since ${day}, ${time}`;
}

/** The three sentences. The copy is the product; it is not filler. */
function verdict(count: number): string {
  if (count === 0) return 'Nothing meaningful happened.';
  if (count === 1) return 'One thing deserves your attention.';
  return `${count} things deserve your attention.`;
}

export function DigestHeader({ digest, connection, action }: Props) {
  const count = digest?.needs_attention.length ?? 0;

  return (
    <header>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: '1rem',
        }}
      >
        <h1
          className="serif"
          style={{ fontSize: '1.5rem', fontWeight: 500, margin: 0, letterSpacing: '-0.01em' }}
        >
          Since
        </h1>
        <span style={{ display: 'flex', alignItems: 'baseline', gap: '0.875rem' }}>
          {connection && <ConnectionDot state={connection} />}
          {action}
        </span>
      </div>

      <p className="muted small" style={{ margin: '1.5rem 0 0' }}>
        {sinceLabel(digest)}
      </p>

      <p
        className="serif"
        style={{
          fontSize: 'clamp(1.75rem, 6vw, 2.5rem)',
          lineHeight: 1.15,
          fontWeight: 400,
          margin: '0.375rem 0 0',
          letterSpacing: '-0.015em',
          maxWidth: '18ch',
        }}
      >
        {verdict(count)}
      </p>
    </header>
  );
}

function ConnectionDot({ state }: { state: 'connected' | 'connecting' | 'offline' }) {
  const label =
    state === 'connected' ? 'Live updates connected' : state === 'connecting' ? 'Connecting' : 'Live updates offline';
  return (
    <span
      className="muted small"
      title={label}
      style={{ display: 'inline-flex', alignItems: 'center', gap: '0.375rem', whiteSpace: 'nowrap' }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: state === 'connected' ? 'var(--up)' : 'var(--rule)',
          flex: '0 0 auto',
        }}
      />
      <span>{state === 'connected' ? 'Live' : 'Offline'}</span>
    </span>
  );
}
