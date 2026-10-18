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

## Last probe result (recorded honestly — not invented)

Run on 2026-10-18 against `https://hurricane.cornelltradingcompetition.org`:

```json
{
  "public_case":    { "ok": true,  "symbols": 12 },
  "auth_time":      { "ok": false, "error": "HTTP 401: {\"detail\":\"Invalid API key\"}" },
  "auth_positions": { "ok": false, "error": "HTTP 401: {\"detail\":\"Invalid API key\"}" }
}
```

### Interpretation

* **Public path works.** `GET /cases/hurricane.json` returns 200 with all 12 symbols. This
  proves the base URL, the required custom `User-Agent`, and the HTTP transport are all
  correct (the exchange refuses urllib's default UA; ours is accepted).
* **Authenticated path is blocked by the credential, not the code.** Both authenticated
  GETs return `401 Invalid API key`. The key in `.env` is well-formed (43 chars, no quotes
  or whitespace) and is sent in the `X-API-Key` header exactly as the packet specifies.
* An earlier WebSocket attempt returned `403` for the same reason (key rejected at the
  front door).

**This is a credential blocker.** Nothing in the client can fix a key the exchange does not
recognize. Required human action before live work:

1. Confirm the team is registered and the bot key is **issued and active** for the testnet
   (captain/portal step).
2. Re-issue or copy the key into `.env` as `CTC_API_KEY=<key>` (no quotes).
3. Re-run `ctc-bot-probe` and confirm `auth_time.ok == true`.

Until `auth_time.ok == true`, do not attempt the order test below — it will only 401.

## First state-changing test — PROPOSED, AWAITING APPROVAL

> **Not yet executed.** No order has been placed or cancelled. This is the precise plan to
> run once (a) the auth probe is green and (b) a human explicitly approves *this specific
> test*.

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
#    Only after a human says "approved: run the PORT place+cancel test".
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

Only then, and with separate approval, proceed to: subscribe to `news`+`quotes`, run
`ctc-bot` in dry-run against the live feed for a full season, verify reconnect across a
season rollover, then consider enabling live sizing. See
[BOT_OPERATIONS.md](BOT_OPERATIONS.md).
