# Testnet Connectivity & First-Order Plan

Status of live-exchange connectivity for the CTC 2026 bot, and the **exact** first
state-changing test that is queued behind explicit human approval.

> Security: the bot key lives only in `.env` (`CTC_API_KEY`, gitignored, mode 600). It is
> never printed, logged, or committed. All tooling redacts it. Do **not** paste it into
> chat, commits, or docs.

## How to run the read-only probe

```bash
pip install -e ".[live]"      # websockets extra
ctc-bot-probe                 # GET-only: public case + authenticated time/positions
ctc-bot-probe --listen 5      # also listen to the WS for 5s (read-only)
```

The probe **never** places, cancels, or modifies anything. It is safe to run anytime.

## Latest read-only probe

Run on 2026-10-24 against the organizer-confirmed `https://cornelltradingcompetition.org`:

```json
{
  "public_case":    { "ok": true,  "symbols": 12 },
  "auth_time":      { "ok": true },
  "auth_positions": { "ok": true }
}
```

Read-only GETs to `/orders/open` and `/orderbook/PORT` also succeeded: there were no open
orders and the PORT book was empty. `/competitions/time` returned:

```json
{
  "competition_id": "c34e2d91-863b-4790-a39d-ad03d6e36864",
  "state": "ready",
  "fictional_time": "2026-08-03T09:30:00",
  "elapsed_active_seconds": 0
}
```

Positions were flat. The season had not started running at the time of the check.

### Host correction and running bot

The configured host is `https://cornelltradingcompetition.org`; the WS URL derived from it
is `wss://cornelltradingcompetition.org/ws/v1/market-data`, matching the starter-kit path.
The earlier 401/403 results were against the obsolete `hurricane.cornelltradingcompetition.org`
host, not evidence of an invalid credential. The API key remains in local `.env` and was
not printed or logged.

One existing `ctc-bot --step-interval 2` process was found; no duplicate bot or WS client was
started. It has an established TLS TCP socket to the exchange's Cloudflare edge. Its
inherited environment has no `CTC_DRY_RUN` override, so the code default is `true`. The
provided running-session context says it subscribed to `news` and `quotes`; fresh terminal
logs/frame counts could not be read from this shell. Since competition state is `ready` and
the PORT book is empty, there is no live book or active season to trade against yet.

## First state-changing test — AUTHORIZED IN CHAT, NOT EXECUTED

> **Not yet executed.** No order has been placed or cancelled. The user authorized this
> single PORT place+cancel test conditional on executable quotes. This is not general order
> authorization. Do not run while the exchange is `ready` or the PORT book is empty.

Goal: place **one** tiny, far-from-market limit order and immediately cancel it, to confirm
the order/cancel round-trip and our parsing — with effectively zero fill risk.

* **Symbol:** `PORT`
* **Side:** `buy`
* **Type:** `limit`
* **Quantity:** `1` (minimum; well under the 200 position limit and our per-order cap)
* **Price:** a limit **far below** the live best bid (e.g. ~50% of the current bid, read
  live from `GET /api/v1/orderbook/PORT` at test time) so it rests and will not fill
* **Then:** `DELETE /api/v1/orders/{order_id}` within a few seconds; verify via
  `GET /api/v1/orders/open` that it is gone and `GET /api/v1/positions` shows PORT = 0

### Exact commands (run only after approval)

```bash
# 1) confirm auth is live
ctc-bot-probe                      # expect auth_time.ok == true

# 2) read the live book to pick a safe, non-marketable price (READ-ONLY)
python -c "import asyncio,json; from ctc.execution.settings import LiveConfig; \
from ctc.execution.client import ExchangeClient; c=ExchangeClient(LiveConfig.from_env()); \
print(json.dumps(asyncio.run(c.get_orderbook('PORT')).__dict__, default=str))"

# 3) THE TEST — place 1 far-below-market buy, then cancel it.
#    Existing authorization applies only to this one-lot test, after live quotes exist.
#    Set <PX> to roughly half of the best bid printed above.
python - <<'PY'
import asyncio
from ctc.execution.settings import LiveConfig
from ctc.execution.client import ExchangeClient
from ctc.execution.models import OrderRequest, Side, OrderType

async def main():
    c = ExchangeClient(LiveConfig.from_env())
    req = OrderRequest("PORT", Side.BUY, 1, OrderType.LIMIT, price=<PX>)  # <PX> = ~half best bid
    ack = await c.place_order(req)
    print("ACK:", ack.order_id, ack.status)
    open_before = await c.get_open_orders()
    print("open after place:", [(o.order_id, o.symbol, o.price) for o in open_before])
    res = await c.cancel_order(ack.order_id)
    print("cancel:", res)
    open_after = await c.get_open_orders()
    print("open after cancel:", [(o.order_id, o.symbol) for o in open_after])
    print("positions:", [(p.symbol, p.quantity) for p in await c.get_positions()])

asyncio.run(main())
PY
```

Expected: an ack with an `order_id` and a resting status; the order appears in
`orders/open`; the cancel succeeds; the order is gone and PORT position is still 0.

If anything deviates (unexpected fill, non-2xx, schema mismatch), stop and record the raw
response here before any further live action.

## After the single order test passes

Only after the round-trip is verified should the staged controller advance beyond
`EXECUTION_TEST`. The one-lot test has not been run; the exchange was last reported `ready`
with no executable PORT quotes. Automatic transition infrastructure is implemented but
must not be enabled for live trading until reliable account equity/PnL and order ownership
semantics are verified against the exchange. See [RISK.md](RISK.md) and
[BOT_OPERATIONS.md](BOT_OPERATIONS.md).
