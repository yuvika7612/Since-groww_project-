"""Scenario runner.

Four scenarios, each demonstrating one claim the product makes. Run with:

    PYTHONPATH=. python3 demo.py

This exists because the interesting behaviour of this system is what it does
when things go wrong, and you cannot ask a live market to produce a stock
split, a stale feed and a bad print on cue.
"""

from __future__ import annotations

from datetime import date, datetime

from app.detect.detector import TickContext, detect
from app.detect.events import ChangeEvent, EventType
from app.detect.signals import SymbolStatistics
from app.digest.service import DigestRow, build_digest, compute_market_context
from app.statistics.compute import restate_statistics
from app.market.validate import reconcile, validate_tick
from app.providers.base import Freshness, Quote

NOW = datetime(2026, 9, 4, 14, 5)
TODAY = date(2026, 9, 4)

# A calm large-cap (1% typical day) and a volatile smallcap (6% typical day).
UNIVERSE: dict[str, tuple[SymbolStatistics, float, str]] = {
    "HDFCBANK": (SymbolStatistics(0.0004, 0.010, 4_000_000, 1780, 1400, 0.95), 1650.0, "HDFC Bank"),
    "TCS":      (SymbolStatistics(0.0003, 0.012, 2_000_000, 4200, 3100, 0.80), 3900.0, "TCS"),
    "INFY":     (SymbolStatistics(0.0002, 0.014, 5_000_000, 1900, 1350, 0.90), 1720.0, "Infosys"),
    "RELIANCE": (SymbolStatistics(0.0005, 0.013, 8_000_000, 3100, 2200, 1.05), 2850.0, "Reliance"),
    "IRFC":     (SymbolStatistics(0.0010, 0.060,   900_000,  230,   65, 1.60), 148.0, "IRFC"),
}


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n  {title}\n{'=' * 68}")


def render(digest) -> None:
    if digest.market and digest.market.is_market_wide:
        print(f"\n  {digest.market.headline()}")

    if digest.needs_attention:
        print(f"\n  NEEDS ATTENTION ({len(digest.needs_attention)})")
        for row in digest.needs_attention:
            print(f"    {row.symbol:<10} {row.price:>9.2f}   {row.primary_reason()}")
    else:
        print("\n  NEEDS ATTENTION (0)")
        print("    Nothing meaningful since you last looked.")

    if digest.quiet:
        print(f"\n  {digest.quiet_summary}")
        print(f"    {', '.join(r.symbol for r in digest.quiet)}")

    if digest.degraded:
        print(f"\n  DATA ISSUES ({len(digest.degraded)})")
        for row in digest.degraded:
            note = row.data_note or f"price is {row.freshness.value}"
            print(f"    {row.symbol:<10} {note}")


def evaluate(moves: dict[str, float], index_return: float) -> list[DigestRow]:
    """Run every symbol through the detector for a given set of moves."""
    rows = []
    for symbol, (stats, prev_close, name) in UNIVERSE.items():
        move = moves.get(symbol, 0.0)
        price = prev_close * (1 + move)
        ctx = TickContext(
            symbol=symbol,
            price=price,
            previous_close=prev_close,
            open_price=prev_close,
            volume_so_far=stats.avg_vol_20d * 0.55,
            session_fraction=0.55,
            index_return=index_return,
            observed_at=NOW,
            session_date=TODAY,
        )
        rows.append(
            DigestRow(
                symbol=symbol,
                name=name,
                price=price,
                freshness=Freshness.LIVE,
                as_of=NOW,
                change_since_seen=move,
                seen_at=NOW,
                events=detect(ctx, stats),
            )
        )
    return rows


# --------------------------------------------------------------------------

rule("1. A quiet session. The output most watchlists cannot produce.")
rows = evaluate({"HDFCBANK": 0.002, "TCS": -0.001, "INFY": 0.004,
                 "RELIANCE": 0.001, "IRFC": 0.015}, index_return=0.001)
render(build_digest(rows, compute_market_context("^NSEI", 0.001, rows), NOW))
print("\n  IRFC moved 1.5%, more than anything else on the list, and is")
print("  correctly reported as quiet: that is a routine day for a stock")
print("  whose typical swing is 6%.")


rule("2. A market-wide selloff. Twelve red rows are one piece of information.")
rows = evaluate({"HDFCBANK": -0.020, "TCS": -0.017, "INFY": -0.019,
                 "RELIANCE": -0.022, "IRFC": 0.001}, index_return=-0.021)
render(build_digest(rows, compute_market_context("^NSEI", -0.021, rows), NOW))
print("\n  Four stocks fell about 2% and are collapsed into the headline,")
print("  because the index already told the user that. IRFC finished flat")
print("  on a day its beta of 1.6 predicted a 3.4% fall, so it is the only")
print("  row that carries new information. A conventional watchlist would")
print("  show four red alerts and render IRFC as unremarkable grey.")


rule("3. A 1:5 stock split. The alert that would end the product's credibility.")
stats, prev_close, _ = UNIVERSE["TCS"]
split_price = prev_close / 5.0
naive_return = (split_price - prev_close) / prev_close
naive_events = detect(
    TickContext("TCS", split_price, prev_close, split_price,
                stats.avg_vol_20d * 0.5, 0.5, 0.0, NOW, TODAY),
    stats,
)
print(f"\n  Quoted price falls {prev_close:.0f} -> {split_price:.0f} overnight.")
print(f"\n  Without corporate action handling, the detector emits:")
for e in naive_events:
    print(f"    [{e.severity:.2f}] {e.explanation}")
print(f"\n  With the split applied to the reference price AND every stored level:")
adjusted_prev = prev_close / 5.0
adjusted_stats = restate_statistics(stats, ratio=5.0)
correct_events = detect(
    TickContext("TCS", split_price, adjusted_prev, split_price,
                adjusted_stats.avg_vol_20d * 0.5, 0.5, 0.0, NOW, TODAY),
    adjusted_stats,
)
corp = ChangeEvent(
    symbol="TCS", type=EventType.CORPORATE_ACTION, severity=0.6,
    occurred_at=NOW, session_date=TODAY,
    explanation="1:5 stock split effective today. Price adjusted, holding unchanged.",
)
for e in correct_events + [corp]:
    print(f"    [{e.severity:.2f}] {e.explanation}")
print("\n  The user is told what happened, not that they lost 80%.")
print("\n  Note: adjusting previous_close alone was not enough. The stored")
print("  52-week range was still in pre-split share terms and produced a")
print("  false range break. A corporate action invalidates every cached")
print("  price level at once. The scenario runner caught this, not review.")


rule("4. Feed failures. Never show a cached price as if it were live.")
stats, prev_close, _ = UNIVERSE["INFY"]

bad = Quote("INFY", prev_close * 1.42, prev_close, prev_close,
            1e6, NOW, "primary")
last_good = Quote("INFY", prev_close, prev_close, prev_close, 1e6, NOW, "primary")
result = validate_tick(bad, last_good, sigma=stats.std_ret_30d, now=NOW)
print(f"\n  Bad print at {bad.price:.2f}:")
print(f"    accepted={result.ok}  reason={result.rejected_reason}")

conflict = reconcile(last_good, secondary_price=prev_close * 1.031)
print(f"\n  Two sources disagree:")
print(f"    {conflict.conflict_note}")

frozen = Quote("INFY", prev_close, prev_close, prev_close, 1e6,
               datetime(2026, 9, 4, 11, 0), "primary")
aged = frozen.aged(NOW, stale_after_seconds=90)
rows = evaluate({}, index_return=0.0)
for row in rows:
    if row.symbol == "INFY":
        row.freshness = aged.freshness
        row.data_note = "feed last updated 11:00, price shown is not current"
render(build_digest(rows, None, NOW))
print("\n  A frozen feed is more dangerous than an outage: the payload looks")
print("  healthy and only the timestamp reveals the problem. It is separated")
print("  out rather than ranked, because its silence is our failure, not the")
print("  market's.")

print()
