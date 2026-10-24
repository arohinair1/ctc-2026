# Risk & Safety Controls

Every order flows through one chokepoint before it can reach the exchange. This document
lists the controls, their defaults, and how to operate the safety switches.

## Layers (in order)

1. **Dry-run gate (default ON).** `CTC_DRY_RUN=true` by default. `ctc-bot --live` now also
   requires `CTC_DRY_RUN=false` and `CTC_AUTO_TRANSITION=true`. Even with all three set,
   `LiveTrader.execute()` stays in dry-run until the in-process transition reaches `LIVE`.
   The rollout starts with a 1% per-order position-limit cap, $10,000 gross exposure, and
   five actions per minute; the 25%, $2,000,000 and 50/min settings remain hard upper bounds.
2. **Kill switch.** `CTC_KILL_SWITCH=true` (or `trader.engage_kill_switch()`) makes the
   `RiskManager` reject every candidate, so nothing is proposed or submitted even in live
   mode. On shutdown, resting orders are cancelled (live mode only).
3. **Pre-trade checks (`RiskManager.evaluate`, pure/synchronous):**
   * kill switch engaged → reject all;
   * stale market data (quote older than `CTC_STALE_AFTER_S`, default 10s) → reject;
   * no valid mid / non-positive quantity / invalid limit price → reject;
   * **position limit** (per-symbol, from `ctc/risk/limits.py`), counting **resting orders**
     on the same side → clamp to remaining room, reject if none;
   * **per-order size cap** = `CTC_MAX_ORDER_SIZE_FRAC` × position limit (default 0.25);
   * **gross exposure cap** = `CTC_MAX_GROSS_EXPOSURE` (default $2,000,000) on projected
     |notional| across the book → reject if exceeded;
   * **duplicate suppression** — same symbol/side/type/price already resting → reject.
4. **Churn control (`LiveTrader`).** A symbol is only re-quoted when the target price moves
   by at least `CTC_MIN_REPRICE_EDGE` (default $0.02) versus the last proposal, cutting order
   spam and spread cost.
5. **Rate limiting (`RateLimiter`).** At most `CTC_MAX_ORDERS_PER_MIN` (default 50, under the
   exchange's 60/bot ceiling) order actions per rolling 60s.
6. **Strategy-level gate.** `decide_live` only trades when the edge exceeds a cushion **plus**
   `uncertainty_k` × the fair-value 1-sigma, so the bot trades mainly when it genuinely knows
   more than the house (e.g. just after a damage report).
7. **Session loss stop.** `CTC_MAX_SESSION_LOSS` defaults to $10,000. A complete, fresh,
   verified session equity snapshot is required. Realized and unrealized PnL are measured
   relative to the opening snapshot. At the threshold, the bot latches its kill switch and
   cancels only order IDs in its persistent registry; it never liquidates positions. Resume
   requires explicit manual authorization.
8. **Persistent ownership and transition.** Submission intent is saved before POST, ACKed
   exchange IDs and execution records are stored atomically under `research/runtime/`, and
   restart always requires reconciliation. Unknown orders, ambiguous submissions, missing
   executions, incomplete PnL, stale quotes, or an unverified PORT test prevent LIVE.
   HALTED / SAFE_MODE requires manual authorization to reset.

## Defaults

| Env var | Default | Meaning |
|---------|--------:|---------|
| `CTC_DRY_RUN` | `true` | no orders submitted unless false |
| `CTC_KILL_SWITCH` | `false` | reject all when true |
| `CTC_MAX_ORDER_SIZE_FRAC` | `0.25` | per-order cap as fraction of position limit |
| `CTC_MAX_GROSS_EXPOSURE` | `2000000` | max |notional| across book ($) |
| `CTC_MAX_ORDERS_PER_MIN` | `50` | order-action rate cap (exchange ceiling 60) |
| `CTC_MIN_REPRICE_EDGE` | `0.02` | min price move to re-quote ($) |
| `CTC_STALE_AFTER_S` | `10` | reject trading on quotes older than this |
| `CTC_MAX_SESSION_LOSS` | `10000` | hard session loss stop ($) |
| `CTC_INITIAL_MAX_GROSS_EXPOSURE` | `10000` | initial rollout gross cap ($) |
| `CTC_INITIAL_MAX_ORDER_SIZE_FRAC` | `0.01` | initial per-order fraction of position limit |
| `CTC_INITIAL_MAX_ORDER_UNITS` | `1` | absolute initial per-order quantity ceiling |
| `CTC_INITIAL_ORDERS_PER_MIN` | `5` | initial order-action cap |
| `CTC_AUTO_TRANSITION` | `false` | require the staged rollout controller for any CLI live session |
| `CTC_ORDER_REGISTRY_PATH` | `research/runtime/order_registry.json` | persistent bot order ownership ledger |
| `CTC_SESSION_PNL_PATH` | `research/runtime/session_pnl.json` | persistent PnL baseline and loss latch |

## Credential handling

* `CTC_API_KEY` is read only from the environment / `.env` (gitignored, mode 600).
* It is never logged, printed, or serialized: `LiveConfig.__repr__` and `.redacted()` mask
  it, HTTP error bodies are passed through `redact()`, and `.env` is excluded from git.
* Verify before any work: `git check-ignore .env` prints a match, `git ls-files .env` is
  empty.

## Operating the switches

```bash
# panic stop (keeps process running but stops new orders):
export CTC_KILL_SWITCH=true          # next cycle rejects everything

# hard stop: Ctrl-C — on shutdown the bot cancels resting orders (live mode only)

# return to safe mode:
unset CTC_DRY_RUN                    # or set CTC_DRY_RUN=true
```

## What is NOT allowed without explicit human approval

Placing, cancelling, or modifying real orders; creating/redeeming ETFs; any other
state-changing exchange call. The first such action is the single place+cancel test in
[TESTNET.md](TESTNET.md), and only after a human approves that specific test.

## Current live-gate limitations

The case packet and starter-kit client currently establish `/positions`, `/orders/open`,
order POST/DELETE and market-data frames. They do **not** document a starting-equity or
realized-fills endpoint, fill execution IDs/prices in a complete history, or an owner field
on open orders. The market trade tape cannot prove which team filled an order. The runner
therefore has no built-in authoritative accounting source; without one, `pnl_complete=false`
and live mode remains blocked.

A team may provide a verified async adapter via `CTC_ACCOUNTING_PROVIDER=module:function`.
The callable receives the configured `ExchangeClient` and must return
`VerifiedPnLSnapshot(starting_equity, realized_pnl, unrealized_pnl, as_of,
source_verified, execution_history_complete, executions)`, with every fill linked to a
bot-owned exchange order ID and a unique execution ID, quantity, and price. The runner
validates freshness, completeness, and registry ownership, and refreshes it every five
seconds. `source_verified` is a trust assertion by that adapter; it must only be true when
the values come from an authoritative, independently checked account source. Do not infer
realized PnL from quote or trade-tape data. The registry can prove ownership of orders
created by this process from acknowledged exchange IDs, but cannot claim ownership of
legacy/manual/team orders. Until the organizers or team provide and verify such an
authoritative source, this gate must remain closed.
