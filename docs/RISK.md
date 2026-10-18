# Risk & Safety Controls

Every order flows through one chokepoint before it can reach the exchange. This document
lists the controls, their defaults, and how to operate the safety switches.

## Layers (in order)

1. **Dry-run gate (default ON).** `CTC_DRY_RUN=true` by default. The bot computes and logs
   every decision but `LiveTrader.execute()` never calls a write endpoint. Going live needs
   **both** `CTC_DRY_RUN=false` and the `ctc-bot --live` flag; otherwise the bot refuses to
   start.
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
