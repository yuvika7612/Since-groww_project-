# CLAUDE.md

Context for Claude Code working in this repo. Read this before changing
anything in `app/`.

## What this is

A submission for Code by Groww (72-hour build challenge). The brief: a
watchlist that shows what has *meaningfully changed* since the user last
checked. Judging weights engineering depth, judgement, edge cases, code
quality, simplicity and originality over feature count. Every design decision
has to be defensible out loud in a live interview.

**Implication for you:** do not add features. Do not "improve" things by making
them more general. If a change cannot be justified in one sentence to a
skeptical engineer, don't make it.

## The three claims the product makes

Every one of these is enforced by tests. If you break a test, you have broken a
product claim, not a detail.

1. **The diff is personal.** `user_symbol_seen` stores a per-user, per-symbol
   read watermark. Change is measured against the price *that user* last saw,
   never against yesterday's close.
2. **Meaningful is relative to the symbol.** Every signal is expressed in units
   of that symbol's own behaviour (z-score vs its 30-day distribution, RVOL vs
   its own 20-day average). Never raw percentages.
3. **The market's move is subtracted.** Scoring uses the residual
   `r_stock − β × r_index`. If the index explains the move, the row is
   collapsed into a headline rather than surfaced.

## Architecture invariant: never break the shared/personal split

```
Shared    sized by instruments (~2000, bounded)
          hot set → poller → validate → detector → change_events
          plus nightly symbol_stats

Personal  sized by users (unbounded)
          watermarks → digest join → client
```

Signal computation happens **once per symbol**, never per user. If you find
yourself writing a loop over users that touches market data, stop — that is the
O(users × symbols) design this system exists to avoid.

## Non-negotiable rules

- **Corporate actions are applied before any return is computed.** A 1:5 split
  is an 80% price drop that means nothing. See `restate_statistics()`: an
  action invalidates every stored *level* (prices, 52w range) and no *ratio*
  (σ, β, mean return). This was a real bug caught by `demo.py`.
- **Never render a cached price as if it were live.** Every `Quote` carries
  `as_of`, `source`, `freshness`. Freshness is recomputed on read, not frozen
  at fetch.
- **Degraded rows are separated before ranking**, never mixed into market
  events. A broken feed is our failure, not the market's.
- **Never average conflicting sources.** Serve the primary exchange, record the
  disagreement. An average of two numbers where one is wrong is a third wrong
  number.
- **No LLM in the ranking path.** Every surfaced row must be explainable by
  stored arithmetic. "The model decided" is not an acceptable answer for a
  product handling money.
- **Silence is a valid output.** A digest with zero rows and "nothing
  meaningful happened" is a success, not an empty state to be filled.

## Style

- Comments explain *why*, never *what*. If a comment restates the code, delete
  it. Existing comments carry the reasoning that has to survive into the
  interview — do not strip them.
- Pure functions in `detect/signals.py`. No DB, no clock, no network there.
- Nothing calls `datetime.now()` directly; go through the provider's `now()` so
  the whole system can run on the replay clock.
- Guard every division. A suspended symbol has σ≈0 and would otherwise produce
  an infinite z-score that dominates the entire digest.

## Commands

```bash
PYTHONPATH=. python3 -m pytest tests/ -q   # 29 tests, all must pass
PYTHONPATH=. python3 demo.py               # four scenarios, no market needed
```

## Built

`detect/` signals, events, detector · `digest/` service · `statistics/`
compute · `market/` validate · `providers/` base, replay · `models.py`

## Not built yet

See `BUILD_PLAN.md` for the full specification of every remaining phase,
including exact API shapes, worker requirements, and frontend design tokens.
Work through it in the stated order and commit after each phase.

Summary: calendar, db/cache glue, digest assembler, API + SSE, workers,
seed/fixtures, React frontend, docker, remaining tests, submission materials.

## Decided (do not re-open without asking)

A row counts as **seen** when it is at least 50% visible in the viewport for
800ms continuously. Not app-open, which would silently clear every unread when
someone opens and closes the app. Not tap-to-detail, which undercounts because
people read lists without tapping. Rationale and implementation are in
BUILD_PLAN.md phase 6.

## Deliberately not built — do not add these

No LLM ranking. No WebSockets (SSE is unidirectional and sufficient). No
candlestick charts, portfolio P&L, or news feed. No microservices. These are
listed in the README as trade-offs and are part of the submission's argument.
