# BUILD_PLAN.md

Everything not yet built, in dependency order. Read `CLAUDE.md` first — it
holds the design decisions this plan assumes. Do not violate them.

**Working agreement:** build one phase, run `pytest`, commit, then move on. Do
not start a later phase while an earlier one is failing. If a phase forces a
change to an earlier decision, stop and say so rather than quietly working
around it.

**Estimated total: ~15 hours.** Phases 1–5 are the backend (~7h), phase 6 is
the frontend (~6h), phases 7–9 are infrastructure and polish (~2h).

---

# Phase 1 — Market calendar (45 min)

## `app/market/calendar.py`

NSE trades 09:15–15:30 IST, with a pre-open auction 09:00–09:15. Everything
time-dependent in this system routes through here.

```python
class MarketState(str, Enum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"      # 09:00-09:15, auction, prices are indicative
    OPEN = "open"              # 09:15-15:30
    POST_CLOSE = "post_close"  # 15:30-16:00, closing session
    HOLIDAY = "holiday"
```

Required functions:

- `market_state(when: datetime) -> MarketState` — treat input as IST. Weekends
  and the holiday list return `HOLIDAY`.
- `is_trading_day(d: date) -> bool`
- `previous_trading_day(d: date) -> date` — used to resolve `previous_close`.
  Must skip weekends *and* holidays; getting this wrong silently corrupts every
  return on the day after a long weekend.
- `session_fraction(when: datetime) -> float` — expected share of a normal
  day's volume traded by this point. See below.
- `poll_interval(state: MarketState) -> int` — seconds, from settings.
  `OPEN`→5, `PRE_OPEN`→30, everything else→600. The poller costs almost nothing
  overnight, which is worth saying out loud when asked about scaling.

### Volume profile

Intraday volume is U-shaped: heavy at the open, thin midday, heavy into the
close. `relative_volume()` in `detect/signals.py` already depends on this and
will read every morning as a volume drought without it.

Implement as a lookup table of cumulative fractions in 15-minute buckets across
the 375-minute session, linearly interpolated between buckets. Reasonable
shape for NSE:

```
09:15  0.00    11:00  0.42    13:00  0.60    15:00  0.86
09:30  0.11    11:30  0.48    13:30  0.66    15:15  0.93
10:00  0.26    12:00  0.53    14:00  0.72    15:30  1.00
10:30  0.35    12:30  0.56    14:30  0.78
```

Add a `TODO` noting this is a static approximation and that the honest version
computes a per-symbol profile from historical intraday bars. Saying you know
the difference matters more than having built it.

### Holidays

Hardcode NSE trading holidays for 2026 as a `frozenset[date]` with a comment
that production would pull from the exchange calendar. Include a
`pandas_market_calendars` fallback path if the package is available.

## Tests — `tests/test_calendar.py`

- State transitions at 09:14:59 / 09:15:00 / 15:29:59 / 15:30:01
- Saturday, Sunday, and a hardcoded holiday all return `HOLIDAY`
- `previous_trading_day` skips a weekend, and skips a holiday adjacent to one
- `session_fraction` is monotonically non-decreasing across the session
- `session_fraction` is 0.0 before open and 1.0 after close
- Midday buckets are flat relative to open and close (proves the U-shape)

---

# Phase 2 — Infrastructure glue (1h)

## `app/db.py`

SQLAlchemy engine and session factory. `init_db()` creates tables. For SQLite,
set `check_same_thread=False` and enable WAL mode, or concurrent reads from the
API and writes from the worker will lock.

Provide `get_session()` as a FastAPI dependency yielding a session and closing
it in a `finally`.

## `app/cache.py`

Thin wrapper with two implementations behind one interface, selected by whether
`settings.redis_url` is set. The in-memory shim means the project runs with
zero infrastructure, which matters when a judge clones it.

```python
class Cache(Protocol):
    def get_quote(self, symbol: str) -> Quote | None: ...
    def set_quote(self, symbol: str, quote: Quote) -> None: ...
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...   # batched
    def add_to_hot_set(self, symbol: str) -> int: ...     # returns new refcount
    def remove_from_hot_set(self, symbol: str) -> int: ...
    def hot_set(self) -> list[str]: ...
    def publish(self, symbol: str, payload: dict) -> None: ...
    def subscribe(self, symbols: list[str]) -> Iterator[dict]: ...
```

`get_quotes` must be a single round trip (Redis `MGET`), not a loop. A user with
50 symbols should cost one call, not 50.

Hot set uses `ZINCRBY`/`ZINCRBY -1` on a sorted set. Symbols with score ≤ 0 are
removed. Refcounting rather than a plain set is what lets the poller stop
polling a symbol the moment the last user drops it.

**In-memory fallback caveat:** it is per-process, so the API and the worker will
not share it. Document this. Running with Redis is required for the two to work
together; the fallback exists so tests and a bare clone still run.

## `app/providers/factory.py`

```python
def build_provider() -> MarketDataProvider:
    if settings.market_provider == "replay":
        return ReplayProvider(settings.replay_fixture, settings.replay_speed)
    return YFinanceProvider()
```

Cache the instance at module level. The replay provider holds clock state, so
constructing a second one resets the demo mid-run.

## `app/providers/yfinance_provider.py`

Implements `MarketDataProvider`.

- Map internal symbols to Yahoo tickers by appending `.NS`. Indices are already
  prefixed (`^NSEI`) and must not get the suffix.
- Batch with `yf.download(tickers=..., period="2d", interval="1m",
  group_by="ticker", threads=True)`.
- **Never raise on partial failure.** Return what succeeded, log the rest. One
  delisted symbol must not kill the poll cycle for the other two thousand.
- Set `freshness=Freshness.DELAYED` and `source="yfinance"`. It is a delayed
  feed and the UI must say so. Labelling a delayed feed as live is exactly the
  dishonesty this system is built to avoid.
- Exponential backoff on rate limiting: 1s, 2s, 4s, 8s, then skip the cycle.
- `previous_close` comes from the *previous trading day* via
  `calendar.previous_trading_day()`, not from `index[-2]`.

---

# Phase 3 — Digest assembler (1h 15m)

`digest/service.py` is pure and stays that way. This is the glue that loads
from the database and cache and produces `DigestRow` objects to feed it.

## `app/digest/assembler.py`

```python
def assemble_digest(session, cache, user_id: int, watchlist_id: int | None,
                    now: datetime) -> Digest:
```

Steps, in order:

1. Load the user's watchlist items. Default to their first watchlist if
   `watchlist_id` is None.
2. Batch-fetch quotes from cache in **one** call.
3. Batch-load watermarks for `(user_id, symbols)` in **one** query.
4. Batch-load `symbol_stats` for those symbols in **one** query.
5. Fetch the benchmark quote (`^NSEI`) and compute `index_return`.
6. Load `change_events` per symbol where `occurred_at > watermark.last_seen_at`.
   Single query with `IN` over symbols, then group in Python. **Do not query
   per symbol** — that is an N+1 and it is the exact anti-pattern this
   architecture exists to avoid.
7. For a symbol with no watermark (never viewed), fall back to the session
   change and mark `seen_at=None` so the UI can label it "new to your list"
   rather than pretending you have a personal baseline.
8. Compute `change_since_seen = (price - watermark.last_seen_price) /
   watermark.last_seen_price`, guarding a zero or null last-seen price.
9. Re-age every quote: `quote.aged(now, settings.stale_after_seconds)`.
   Freshness is a property of the moment you look, not of the fetch.
10. Build `MarketContext` via `compute_market_context`.
11. Call `build_digest`.

**Personal events.** `CROSSED_COST_BASIS` cannot live in `change_events`
because it depends on the user's `cost_basis`. Compute it here, per user, and
append to `row.events` before ranking. Keep it out of the shared table — that
boundary is the architecture.

## `app/digest/seen.py`

```python
def mark_seen(session, user_id: int, entries: list[SeenEntry], now: datetime) -> int:
```

Where `SeenEntry` is `{symbol, seen_at, price}`.

- Upsert with **monotonic** semantics:
  `last_seen_at = GREATEST(existing, incoming)`. Postgres:
  `ON CONFLICT ... DO UPDATE SET last_seen_at = GREATEST(...)`. SQLite: use
  `INSERT ... ON CONFLICT DO UPDATE` with a `MAX()` expression. Write a helper
  that picks the right dialect.
- Only update `last_seen_price` when `last_seen_at` actually advances,
  otherwise a late-arriving batch from a stale tab rewrites the price with an
  old one while leaving the newer timestamp.
- Reject `seen_at` more than 60s in the future (client clock skew). Clamp to
  server time rather than erroring — the user did nothing wrong.
- Idempotent: replaying the same batch changes nothing.

**This is the single most important correctness property in the product.** If
watermarks go backwards, "since you last checked" is a lie. Test it hard.

## Tests — `tests/test_assembler.py`, `tests/test_seen.py`

- A never-seen symbol falls back to session change with `seen_at=None`
- Watermarks never move backwards, in any batch ordering
- Replaying an identical batch is a no-op
- Out-of-order batches converge to the latest timestamp
- Future timestamps are clamped, not rejected
- `last_seen_price` does not regress when the timestamp does not advance
- Assembling a digest for 50 symbols issues a bounded number of queries — count
  them with a SQLAlchemy event listener and assert `< 8`. This test is what
  stops someone reintroducing an N+1 later.

---

# Phase 4 — API (2h 30m)

## `app/schemas.py`

Pydantic response models. Explicit shapes, no ORM leakage. Every price-bearing
response carries `as_of`, `source` and `freshness`.

## `app/api/routes.py`

### Auth

Keep it minimal and be explicit that it is minimal.

- `POST /api/auth/dev-login` — body `{email}`, creates the user if absent,
  returns `{user_id, token}` where the token is a signed value carrying the
  user id. A `get_current_user` dependency reads it from the `Authorization`
  header.

Document in the README: real auth was out of scope for 72 hours, the interface
is isolated behind one dependency, and swapping in OAuth touches one function.
That is a better answer than a half-built password system.

### Watchlists

```
GET    /api/watchlists                      → [{id, name, item_count}]
POST   /api/watchlists                      {name} → watchlist
GET    /api/watchlists/{id}                 → watchlist with items
DELETE /api/watchlists/{id}
POST   /api/watchlists/{id}/items           {symbol, cost_basis?, note?}
DELETE /api/watchlists/{id}/items/{symbol}
PATCH  /api/watchlists/{id}/items/{symbol}  {cost_basis?, note?}
```

On add: increment the hot set, set `price_at_add` from the current quote,
return 409 on duplicates. On remove: decrement the hot set.

Every route must verify the watchlist belongs to the requesting user. Return
404 rather than 403 for another user's resource — 403 confirms the id exists.

### Symbols

```
GET /api/symbols/search?q=rel&limit=10   → [{symbol, name, exchange}]
GET /api/symbols/{symbol}                → detail + stats + recent events
```

Search on symbol prefix and name substring, case-insensitive. Cap `limit` at 50
server-side; never trust a client-supplied limit.

### The two that matter

```
GET  /api/digest?watchlist_id=              → Digest
POST /api/seen                              {entries: [{symbol, seen_at, price}]}
```

`GET /api/digest` response shape:

```json
{
  "generated_at": "2026-09-04T14:05:00+05:30",
  "market_state": "open",
  "market": {
    "index_symbol": "^NSEI",
    "index_return": -0.021,
    "breadth": 0.8,
    "is_market_wide": true,
    "headline": "Market down 2.1%. 80% of your list moved with it."
  },
  "needs_attention": [
    {
      "symbol": "IRFC",
      "name": "IRFC",
      "price": 148.15,
      "as_of": "2026-09-04T14:04:58+05:30",
      "source": "yfinance",
      "freshness": "delayed",
      "change_since_seen": 0.001,
      "seen_at": "2026-09-02T10:12:00+05:30",
      "score": 0.61,
      "primary_reason": "Moved against the market: outperformed the index by 3.5% after adjusting for beta 1.60",
      "events": [ { "type": "...", "severity": 0.58, "explanation": "...", "payload": {} } ]
    }
  ],
  "quiet": [ ... ],
  "quiet_summary": "4 other symbols: nothing meaningful.",
  "degraded": [ { "...": "...", "data_note": "feed last updated 11:00, price shown is not current" } ]
}
```

`POST /api/seen` takes a **batch**. The frontend sends one request per debounce
window, not one per row. Returns `{updated: n}`.

### SSE

```
GET /api/stream?symbols=TCS,INFY
```

- `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`
  (nginx buffers SSE by default and will silently break it).
- Only subscribe to symbols the user actually watches — verify server-side, do
  not trust the query string.
- Heartbeat comment (`: ping`) every 15s so proxies do not time out an idle
  connection.
- Event types: `quote` (price update), `event` (new change event), `market`
  (index / market state change).
- Send `id:` on each message and honour `Last-Event-ID` on reconnect. This is
  the whole reason SSE was chosen over WebSockets — reconnection is in the
  protocol. If you do not implement it, the choice loses its justification.
- Close the subscription in a `finally` when the client disconnects, or the
  hot set refcount leaks.

### Demo endpoints

Gate behind `settings.market_provider == "replay"` and return 404 otherwise.

```
POST /api/debug/inject-fault  {kind, symbol?, magnitude?, duration_minutes?}
POST /api/debug/seek          {to: "2026-09-04T09:30:00"}
GET  /api/debug/scenarios     → list of preset scenarios for the demo UI
```

Presets: `quiet_day`, `market_selloff`, `split`, `feed_outage`, `bad_tick`,
`frozen_feed`. Each seeks the clock and schedules the right faults. **This is
what you drive the live demo from.**

### Health

```
GET /health   → {status, market_state, hot_set_size, last_poll_at,
                 stale_symbol_count, provider}
```

`last_poll_at` is what tells you at a glance whether the worker died.

## `app/main.py`

FastAPI app, CORS for the Vite dev origin, `init_db()` on startup, mount
routes, global exception handler returning a consistent error shape.

## Tests — `tests/test_api.py`

Use `fastapi.testclient` against SQLite with the replay provider. Cover: full
watchlist CRUD, cross-user access returns 404, duplicate add returns 409,
digest shape, seen idempotency, hot set increments and decrements on add and
remove.

---

# Phase 5 — Workers (1h 30m)

## `workers/ingest.py`

```
loop:
    state = market_state(provider.now())
    if state is CLOSED or HOLIDAY: sleep(poll_interval); continue
    symbols = cache.hot_set()
    for batch in chunks(symbols, 50):
        quotes = provider.fetch(batch)
        for quote in quotes:
            stats = stats_cache[symbol]
            result = validate_tick(quote, cache.get_quote(symbol),
                                   stats.std_ret_30d, provider.now())
            if not result.ok:
                metrics.rejected += 1; log(reason); continue
            result = reconcile(result.accepted, secondary.get(symbol))
            cache.set_quote(symbol, result.accepted)
            events = detect(build_context(...), stats)
            events = dedupe(events, session_keys)
            persist(events)               # unique dedupe_key absorbs races
            cache.publish(symbol, ...)    # drives SSE
    sleep(poll_interval(state))
```

Requirements:

- **Chunk upstream calls.** 50 symbols per request.
- **`symbol_stats` cached in memory**, refreshed once per session, not read
  from the database per tick.
- **Never let one symbol kill the cycle.** Wrap per-symbol work in
  `try/except`, log, continue.
- **Graceful shutdown** on SIGTERM: finish the current batch, close the DB.
- **Idempotent on restart.** The unique constraint on `dedupe_key` means a
  restarted worker cannot double-emit. Catch `IntegrityError` and move on.
- Log a one-line cycle summary: symbols polled, quotes accepted, ticks
  rejected, events emitted, duration.
- `previous_close` from `calendar.previous_trading_day()`, never `now - 1 day`.

## `workers/nightly.py`

Runs after the close, or on demand.

1. Fetch daily bars for every symbol in the hot set (60+ sessions).
2. Fetch and upsert corporate actions.
3. `compute_symbol_statistics(bars, index_bars, actions)` per symbol.
4. **Apply `restate_statistics()` for any action with an ex-date since the last
   run.** A corporate action invalidates every stored price level at once —
   this is the bug the scenario runner caught. Do not skip it.
5. Upsert `symbol_stats` with `sample_size` and `computed_at`.
6. Log symbols where `sample_size < 30`, since their beta fell back to 1.0.

Idempotent: running it twice produces the same rows.

## `scripts/seed.py`

Seeds ~30 liquid NSE symbols across sectors plus `^NSEI`, 90 days of daily
bars, a demo user with a watchlist, and one deliberately planted corporate
action so the split scenario works out of the box.

Must run offline. If the provider is unavailable, generate synthetic bars from
a seeded random walk with per-symbol volatility, so a clean clone always works.

## `scripts/record_fixture.py`

Writes `data/session.jsonl` for the replay provider:

```json
{"symbol":"TCS","as_of":"2026-09-04T09:15:00","price":3900.0,"open":3900.0,"previous_close":3895.0,"volume":120000}
```

Two modes: record a live session, or synthesise one. The synthesiser must be
able to produce each scenario deliberately — a quiet day, a market-wide
selloff with one stock holding up, and a split day. **The selloff fixture is
what you demo**, so build that one first and check it actually produces the
collapse.

---

# Phase 6 — Frontend (6h)

Vite + React + TypeScript. TanStack Query for server state. No Redux; there is
almost no client state here.

Generate types from the OpenAPI schema (`openapi-typescript`) rather than
hand-writing them. It is five minutes and it stops the two halves drifting.

## Design direction

The metaphor is a **reading queue, not a trading terminal.** The product's
claim is that most of the time nothing happened and the correct output is
silence. A dense grid of flashing tickers contradicts that on sight. Build
something closer to a well-set reader.

### Tokens

```
--paper      #FFFFFF     surface
--ink        #16191D     primary text
--muted      #5C6470     secondary text, timestamps
--rule       #E4E6EA     hairlines, dividers
--unseen     #2B5CE6     the unread marker, and only that
--caution    #8A6A1F     degraded data only, never price direction
--up         #1A7F5A     text colour only
--down       #B4342A     text colour only
```

**Do not use red/green fills, backgrounds or tints.** Direction appears as the
number's text colour and a small glyph, nothing more. This is a deliberate
choice you should be ready to defend: the whole product argues that the size of
a colour is not the size of the meaning, and an interface that shouts in
proportion to the percentage move undoes the ranking work the backend just did.
It also happens to survive colour-blindness and direct sunlight on a cheap
Android screen.

Severity is carried by **position and typographic weight**, not by colour. The
top row is the most important because it is at the top.

### Type

Two families, clearly distinct:

- **Newsreader** (serif) for the digest headline and the verdict sentence. The
  reading-queue metaphor, and it makes the headline an element rather than a
  label.
- **IBM Plex Sans** for UI and data. It has true tabular figures, which matter
  the moment prices sit in a column — proportional digits make a price column
  visibly ragged.

Set `font-variant-numeric: tabular-nums` on every price and percentage.

### Layout

Single column, max ~68ch, left aligned. Not a dashboard grid.

```
┌──────────────────────────────────────────┐
│  Since Tuesday, 10:12                    │  ← muted, when you last looked
│                                          │
│  Nothing meaningful                      │  ← serif, large. THE HERO.
│  happened.                               │
│                                          │
│  Market down 2.1%. 80% of your list      │  ← only when market-wide
│  moved with it.                          │
│  ────────────────────────────────────    │
│                                          │
│  IRFC                    148.15  +0.1%   │  ← attention rows
│  Moved against the market: outperformed  │  ← the reason, always visible
│  the index by 3.5% (beta 1.60)           │
│  ────────────────────────────────────    │
│                                          │
│  4 others: nothing meaningful      ⌄     │  ← collapsed, expandable
│                                          │
│  ⚠ INFY — feed last updated 11:00        │  ← degraded, visually separate
└──────────────────────────────────────────┘
```

The hero is the verdict sentence, not a number and not a chart. On a quiet day
the entire screen is "Nothing meaningful happened" plus a collapsed list. **Do
not treat that as an empty state to be filled.** It is the product working.

### Components

```
src/
  api/client.ts              typed fetch wrapper, auth header
  api/types.ts               generated from OpenAPI
  hooks/useDigest.ts         TanStack Query, refetch on window focus
  hooks/useSeenTracking.ts   IntersectionObserver, debounced batch POST
  hooks/useMarketStream.ts   SSE with reconnect
  components/
    DigestHeader.tsx         "Since Tuesday, 10:12" + the verdict sentence
    MarketHeadline.tsx       renders only when is_market_wide
    AttentionRow.tsx         symbol, price, change-since-seen, reason
    QuietSection.tsx         collapsed count, expands to a plain list
    DegradedSection.tsx      staleness notices, visually separate
    FreshnessBadge.tsx       live / delayed / stale, always visible
    SymbolSearch.tsx         debounced, keyboard navigable
    WatchlistManager.tsx     add, remove, reorder, cost basis, note
    ScenarioPanel.tsx        demo controls, replay mode only
  pages/
    Digest.tsx
    Manage.tsx
    SymbolDetail.tsx
```

### `useSeenTracking` — the important one

The decision: a row counts as seen when it is **≥50% visible in the viewport
for 800ms continuously**.

Why not the alternatives, since you will be asked:

- *App open* overcounts badly. Opening and closing the app would silently clear
  every unread, which breaks the product's central promise.
- *Tap into detail* undercounts. People read the list without tapping, then the
  row stays flagged forever and the digest fills with things they have already
  read.

Implementation:

```
IntersectionObserver(threshold: 0.5)
  on enter → setTimeout(800ms) → add {symbol, seen_at: now, price} to buffer
  on exit before 800ms → clear the timer
buffer flushes on: 2s debounce, page hide (visibilitychange), or beforeunload
flush via POST /api/seen; on failure keep the buffer and retry next flush
optimistic: mark locally immediately, reconcile on next digest fetch
```

Use `navigator.sendBeacon` for the `visibilitychange` flush — a normal fetch
gets cancelled when the tab is backgrounded on mobile, which is exactly when
you most need the write to land.

Respect `prefers-reduced-motion` and pause tracking when the document is
hidden.

### `useMarketStream`

- `EventSource` to `/api/stream`, subscribing only to visible symbols.
- Reconnect with exponential backoff and jitter, capped at 30s.
- On reconnect, refetch the digest — SSE gives you deltas, and a gap means your
  local state is untrustworthy.
- Stop the stream when the market is closed. A socket held open all night for
  data that cannot change is waste, and closing it is a point worth making.
- Show a small connection indicator. When the stream is down, say so.

### Freshness in the UI

Non-negotiable and directly enforceable in review:

- `live` — no badge, the default
- `delayed` — a quiet "15m delayed" label next to the price
- `stale` — the price renders in `--muted` with the last-updated time. **It
  must not look like a live price.**
- `unavailable` — no number at all. An em dash and a reason. Never a last-known
  price dressed up as current.

### Accessibility floor

Keyboard reachable throughout with visible focus rings. `aria-live="polite"` on
the digest so a screen reader announces new attention rows. Direction never
carried by colour alone — always a glyph or a sign. Contrast ≥ 4.5:1
throughout. Responsive to 360px.

### Copy

Plain sentences, sentence case, no all-caps labels. "Nothing meaningful
happened," not "NO ALERTS." "Feed last updated 11:00," not "STALE DATA ERROR."
Errors explain what happened and what to do; they do not apologise.

---

# Phase 7 — Infrastructure (45 min)

## `docker-compose.yml`

Four services: `postgres:16`, `redis:7`, `api`, `worker`. The worker depends on
both stores. Healthchecks on the databases so the API does not start against a
half-initialised Postgres. Named volume for Postgres.

## `backend/Dockerfile`

`python:3.12-slim`, non-root user, requirements layer cached before the source
copy.

## `frontend/Dockerfile`

Multi-stage: node build, then `nginx:alpine` serving `dist`. The nginx config
must set `proxy_buffering off` on the SSE route or the stream silently breaks
in production while working perfectly in dev.

## `.env.example`

Every setting from `config.py` with safe defaults and a comment each.

## `Makefile`

`make setup`, `make test`, `make demo`, `make dev`, `make seed`, `make lint`.

---

# Phase 8 — Tests to add (30 min)

Target ~55 tests total, up from 29.

- `test_calendar.py` — as specified in phase 1
- `test_seen.py` — monotonicity under every batch ordering (highest value)
- `test_assembler.py` — including the query-count ceiling
- `test_api.py` — CRUD, authorisation, digest shape, seen idempotency
- `test_validate.py` — bad ticks, conflicts, future timestamps, zero prices
- `test_replay.py` — virtual clock advances, each fault kind fires
- `test_ingest.py` — one broken symbol does not stop the cycle; restart does
  not double-emit

Add a `test_scenarios.py` that runs each demo preset end to end and asserts the
digest matches expectations. That test is what stops a refactor quietly
breaking your presentation.

---

# Phase 9 — Submission (30 min)

## The 100-word pitch

Draft, needs tightening to exactly 100:

> Most watchlists show you the market. This one shows what changed for you.
> A per-user read watermark measures change against the price you last actually
> saw, not yesterday's close. Every move is scored against that stock's own
> volatility, so a 4% day counts for a large-cap bank and not for a smallcap
> that swings 6% routinely. Moves the index already explains collapse into one
> headline, surfacing only what moved differently — including the stock that
> stayed flat while the market fell. Corporate actions are adjusted before any
> return is computed, stale feeds never render as live, and silence is a
> supported answer.

## README additions

The existing README is close to final. Add:

- Setup and run instructions for the full stack
- A screenshot of the quiet-day digest, since it is the least expected output
- The demo script: which scenario to run, in what order, and what to point at
- A short "what I would do with another week" section — per-symbol volume
  profiles, sector-relative residuals in addition to index-relative, real auth,
  push notifications. Naming what you did not get to reads as judgement, not
  as a gap.

## Demo run sheet

Five minutes, in this order:

1. **Quiet day.** The screen says nothing meaningful happened. Explain that
   IRFC moved 1.5%, more than anything else, and is correctly silent.
2. **Market selloff.** Four rows collapse into one headline; IRFC surfaces
   because it stayed flat when its beta predicted a 3.4% fall.
3. **Split.** Show the four false alerts without adjustment, then the single
   correct event with it. Mention that the 52-week range bug was caught by the
   scenario runner, not by review.
4. **Frozen feed.** The row goes visibly stale. Explain why a frozen feed is
   more dangerous than an outage.
5. **Bad tick.** Quarantined, with the sigma band shown.

Rehearse it. The demo is the argument.

---

# Order of work

1. Calendar — everything time-dependent needs it
2. Infrastructure glue — db, cache, provider factory
3. Assembler and seen — the core logic
4. API — unblocks the frontend
5. Seed and fixtures — makes the API return real data
6. Frontend — longest single piece
7. Workers — the API works from cache without them, so they can come late
8. Docker, tests, submission

**If you run short on time, cut in this order:** frontend polish first, then the
yfinance provider (replay alone is defensible and demos better), then docker,
then SSE (fall back to polling on a 10s interval). Do not cut: seen
monotonicity, corporate action handling, or staleness rendering. Those three
are the submission's argument.
