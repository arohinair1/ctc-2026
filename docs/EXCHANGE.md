# Exchange Protocol Notes

> Update, 2026-10-24: the team reported HTTP 200 after switching to
> `https://cornelltradingcompetition.org`; earlier key-blocker statements below
> refer to the old hurricane subdomain and are superseded. Defaults are fixed;
> local `.env` overrides must also be updated. Authenticated feed/order behavior
> remains unverified by this agent. See [quote-aware review](QUOTE_AWARE_REVIEW.md).


What the client implements against the Archipelago Exchange, and — importantly — which parts
are **confirmed** from the case packet / starter kit versus **assumed** (tolerant parsing)
and must be verified against a live feed. Do not invent endpoints or message shapes beyond
what is written here; update this file when a live session confirms or corrects a field.

## Base URLs & auth

* HTTP base: `https://cornelltradingcompetition.org` (override via `CTC_HTTP_URL`).
* REST API is under `/api/v1`. The static case file is **public** at
  `GET /cases/hurricane.json` (no `/api/v1` prefix) — confirmed working (200, 12 symbols).
* WebSocket: `wss://.../ws/v1/market-data` (derived from the HTTP URL).
* Every request sends:
  * `X-API-Key: <bot key>` — confirmed required.
  * `User-Agent: <custom>` — **required**; the exchange refuses urllib's default UA
    (confirmed: custom UA returns 200 on the public endpoint).

## REST endpoints used (`src/ctc/execution/client.py`)

| Method | Path | Purpose | Status |
|--------|------|---------|--------|
| GET | `/cases/hurricane.json` | Static case (limits, symbols) | Confirmed 200 |
| GET | `/competitions/time` | Server time / round phase | Confirmed 200 on corrected host (2026-10-24) |
| GET | `/positions` | Current positions | Confirmed 200 on corrected host (2026-10-24) |
| GET | `/orders/open` | Resting orders | Confirmed 200 on corrected host (2026-10-24) |
| GET | `/orderbook/{symbol}` | Depth snapshot | Confirmed 200; PORT book was empty (2026-10-24) |
| POST | `/orders` | Place an order | Not yet exercised (gated) |
| DELETE | `/orders/{id}` | Cancel one order | Not yet exercised (gated) |
| DELETE | `/orders/all[?symbol=]` | Cancel all (optionally per symbol) | Not yet exercised (gated) |
| POST | `/etf/{symbol}/create` | Create ETF units | Not yet exercised (gated) |
| POST | `/etf/{symbol}/redeem` | Redeem ETF units | Not yet exercised (gated) |

> Earlier 401 results were against the obsolete `hurricane.cornelltradingcompetition.org`
> hostname. The organizer-confirmed host above now authenticates successfully. State-changing
> endpoints remain gated and have not been exercised.

### Order payload (`OrderRequest.to_payload`)

```json
{ "symbol": "PORT", "side": "buy", "order_type": "limit", "quantity": 10, "price": 72.123456 }
```

* `side` ∈ {`buy`, `sell`}; `order_type` ∈ {`limit`, `market`}.
* `price` included only for limit orders, rounded to 6 decimals (packet: up to 6 dp).

## WebSocket market data

* Send exactly **one** `subscribe` frame within ~10s of connecting; one JSON message per
  frame:

  ```json
  { "action": "subscribe", "symbols": ["PORT", "..."], "channels": ["news", "quotes"] }
  ```
* Channels used: `news`, `quotes` (also available per packet: `trades`, `orderbook`).
* Frame shapes the client parses (`models.Frame` / `state.LiveState`):
  * `news` frames wrap a feed message: `{ "type": "news", "message": { ... } }`. The inner
    message matches the research `feed.jsonl` schema (storm, track, advisory, impact,
    damage_report, snapshot, ...) and is fed straight into the `SeasonEstimator`.
  * `quote` frames: `{ "type": "quote", "symbol": ..., "bid": ..., "ask": ... }`.
  * `orderbook` frames: `{ "type": "orderbook", "symbol": ..., "bids": [...], "asks": [...] }`.
* On disconnect the client reconnects with exponential backoff and yields a `_reconnect`
  sentinel so the bot triggers a REST resync; a `snapshot` message catches the estimator up
  idempotently (by round, so damage reports are never double-counted).

## Rate limits & caps (enforced on our side)

* 60 order actions / min / bot (180 / team) → `RateLimiter` default 50/min (headroom).
* 100 requests / s / team, 25 market-data connections / team — respected by keeping a single
  stream and modest polling.
* 200 resting orders / symbol; per-symbol position limits — enforced in `ctc/risk/limits.py`
  and `RiskManager` (counts resting orders toward the limit).

## Season rollovers (testnet)

The testnet replays research seasons in sequence. At a new season the competition id changes,
positions reset to zero, and `seq` restarts. `LiveState` detects a round-1 report after a
later round and calls `on_new_season()` (estimator reset, seq set cleared, positions/orders
cleared); the bot re-subscribes on reconnect.

## Confirmed vs assumed — action items for the first live session

* **Confirmed:** corrected base URL, `/api/v1` prefix, public case path, custom-UA
  requirement, `X-API-Key` header, and authenticated GET access on 2026-10-24.
* **Assumed / tolerant (verify live):** exact field names on `quote`/`orderbook`/`position`/
  `order` beyond `symbol`/`bid`/`ask`/`quantity`/`price`; the `snapshot` payload layout; the
  ack schema (`order_id` vs `id`, `status`, `filled` vs `filled_quantity`). Parsers accept
  common variants; confirm the real names during the first authenticated session and tighten
  here.
