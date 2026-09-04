/** Twelve red rows are one piece of information.
 *
 * Renders only when the move really was market-wide. A large index move with
 * poor breadth is a few heavyweights dragging the average, and in that case
 * the individual rows genuinely are the story.
 */
import type { MarketOut } from '../api/client';

export function MarketHeadline({ market }: { market: MarketOut | null | undefined }) {
  if (!market || !market.is_market_wide) return null;
  return (
    <p style={{ margin: '1.25rem 0 0', maxWidth: '46ch' }}>
      {market.headline}
    </p>
  );
}
