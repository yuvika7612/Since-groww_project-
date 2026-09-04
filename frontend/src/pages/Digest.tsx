import { Link } from 'react-router-dom';
import { useDigest, useHealth } from '../hooks/useDigest';
import { useSeenTracking } from '../hooks/useSeenTracking';
import { useMarketStream } from '../hooks/useMarketStream';
import { DigestHeader } from '../components/DigestHeader';
import { MarketHeadline } from '../components/MarketHeadline';
import { AttentionRow } from '../components/AttentionRow';
import { QuietSection } from '../components/QuietSection';
import { DegradedSection } from '../components/DegradedSection';
import { ScenarioPanel } from '../components/ScenarioPanel';
import { useState } from 'react';

export function Digest() {
  const { data: digest, isLoading, error } = useDigest();
  const { data: health } = useHealth();
  const [seen, setSeen] = useState<Set<string>>(new Set());

  const symbols = digest
    ? [...digest.needs_attention, ...digest.quiet, ...digest.degraded].map((r) => r.symbol)
    : [];

  const connection = useMarketStream(symbols, digest?.market_state);

  const { track } = useSeenTracking({
    onSeen: (symbol) => setSeen((current) => new Set(current).add(symbol)),
  });

  return (
    <div className="shell">
      <DigestHeader
        digest={digest}
        connection={connection}
        action={
          <Link to="/manage" className="small">
            Manage list
          </Link>
        }
      />
      <MarketHeadline market={digest?.market} />

      {/* Announced politely: a screen reader should learn that new rows
          arrived without having the page yanked out from under it. */}
      <main aria-live="polite" aria-busy={isLoading} style={{ marginTop: '1.75rem' }}>
        {isLoading && <p className="muted">Reading the market...</p>}

        {error && (
          <p style={{ color: 'var(--caution)' }}>
            Could not load your digest. It will retry on its own.
          </p>
        )}

        {digest?.needs_attention.map((row) => (
          <AttentionRow
            key={row.symbol}
            row={row}
            trackRef={track(row.symbol, row.price ?? null)}
            unread={row.seen_at === null && !seen.has(row.symbol)}
          />
        ))}

        {digest && (
          <div style={{ marginTop: '1.75rem', display: 'grid', gap: '1.75rem' }}>
            <QuietSection rows={digest.quiet} summary={digest.quiet_summary} />
            <DegradedSection rows={digest.degraded} />
          </div>
        )}
      </main>

      <ScenarioPanel provider={health?.provider} />
    </div>
  );
}
