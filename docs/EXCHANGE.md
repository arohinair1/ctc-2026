# Exchange Protocol Notes

## Live startup fixes verified on 2026-10-24

The official web client and authenticated read-only probes confirmed:
`GET /positions?include_zero=true` includes cumulative `realized_pnl` and
`unrealized_pnl`, including closed positions (flat rows have null unrealized PnL).
`GET /orders?limit=500` exposes account order IDs, quantities, fill quantities and
terminal status. `GET /system/bot-trading` exposes bot availability. These routes
were found in the official web client, not inferred from the public trade tape.

Set `CTC_ACCOUNTING_PROVIDER=ctc.execution.accounting:read_snapshot` to use these
verified account fields. This provider tracks the change in authoritative cumulative
account PnL from a persisted session baseline; it does not invent starting equity or
fill prices. Order states are reconciled by acknowledged ID; omitted bot IDs, invalid
fields, stale snapshots and ambiguous submissions continue to block live trading.

For an explicitly shared manual account, `CTC_PRESERVE_EXTERNAL_ORDERS=true` preserves
orders outside this bot's ACK registry, includes all account positions/open orders in
risk calculations, and never cancels external orders. Default remains false. Earlier
unresolved external IDs require explicit operator reconciliation before restarting.
Account PnL and the session loss stop include manual trades as well as bot trades.

Inherited holdings over the initial gross cap may be reduced, but increases remain
blocked. The PORT one-share passive test chooses a sell above the ask for an inherited
long, otherwise a buy below the bid. Both sides require ACK, matching open-order readback,
confirmed cancellation, disappearance and unchanged position. Initial limits remain
one share/order, five actions/minute and $10,000 gross; dry-run remains the default.

Quiet quotes are refreshed from actual REST books rather than assuming every symbol
receives a heartbeat. Initial snapshots restore prior final damage reports. Unsent
observation proposals do not consume churn prices. Strategy orders must have positive
executable edge; a flat target alone is not a profitable liquidation signal. The rate
limiter no longer recursively acquires its own lock, and budget exhaustion keeps the
feed loop running instead of blocking it.

To stop an instance cooperatively, create a file beside its lock with suffix `.stop`
(e.g. `research/runtime/bot.stop`). The loop exits and cancels only its own tracked orders.
Remove that stop file deliberately before restarting. Do not delete a held lock.

Validation: 197 tests with the research dataset, Ruff lint/format, authenticated account
reconciliation, all-symbol market feed, actual passive PORT place/cancel with zero fills,
and exchange-confirmed strategy fills. No credentials or account recordings are committed.

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

The reviewed case packet and starter-kit implementation do not document an account-equity,
realized-fills/history, or execution-report endpoint. `/positions` currently parses quantity,
mark and optional `unrealized_pnl`; `/orders/open` exposes resting order details and an
optional filled quantity, but neither supplies a complete realized-PnL ledger. Public
`trades` frames have no documented bot order ownership link. Do not use market tape or
submitted limit prices as substitutes for execution records.

`ctc.execution.automation.BotOrderRegistry` persists our own submission intent before POST,
then ACK order IDs and explicit execution/cancellation records. On restart, every active
registered ID must match `/orders/open`; unmatched exchange IDs and locally-active IDs
missing from the exchange response leave ownership unresolved. A partial `filled` field
without execution price is reconciled as quantity only and does not make PnL complete.
`SessionPnLTracker` requires a fresh, verified starting equity plus realized and unrealized
PnL snapshot and fails closed on missing, stale or inconsistent input.

The runner accepts an optional team-owned async accounting adapter configured as
`CTC_ACCOUNTING_PROVIDER=module:function`. It receives the existing client and returns the
typed `VerifiedPnLSnapshot` defined in `ctc.execution.automation`, including a complete
execution-history flag and records tied to acknowledged bot order IDs. No exchange endpoint
is assumed for this adapter. The runner does not reach live eligibility until the adapter
provides a fresh verified snapshot and the persistent registry reconciles every fill.
The completeness assertion is refreshed after restart; persisted state alone is not evidence.

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
