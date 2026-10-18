# Handoff — CTC 2026 Systematic Equities

Status of the research **and live-bot** baseline on branch `feat/research-baseline`, and what
is left before and during the event. Companion docs: [ARCHITECTURE.md](ARCHITECTURE.md),
[EXCHANGE.md](EXCHANGE.md), [BOT_OPERATIONS.md](BOT_OPERATIONS.md), [RISK.md](RISK.md),
[TESTNET.md](TESTNET.md), [CHECKLIST.md](CHECKLIST.md), [RESEARCH.md](RESEARCH.md).

## TL;DR for whoever resumes

* Research (valuation + forecasting + backtest) is **done and verified**.
* The **live stack is built and tested offline**: async exchange client (REST+WS, reconnect,
  rate limit), live state (seq dedup, season rollover, snapshot resync), risk manager, trader
  (dry-run/kill-switch gated), recorder/replay, and probe. **128 tests pass; lint clean.**
* The only blocker to live work is a **valid/active bot key** — the current key returns
  `401 Invalid API key` (public endpoint works). See [TESTNET.md](TESTNET.md).
* **Do not place/cancel real orders without explicit human approval.** The exact first test
  (1-lot PORT place+cancel, far from market) is written up in [TESTNET.md](TESTNET.md).

## What is done

* **Phase 1–3** — project scaffolding, typed data loader/validator, wind + outage model,
  fundamental valuation models for all 12 symbols.
* **Phase 4–5** — forecast/information-quality analysis, leakage-safe `SeasonEstimator`,
  event-driven backtester + baseline strategy + risk limits.
* **Phase 6 (research)** — automated test suite + GitHub Actions CI (lint + format + pytest
  on Python 3.10–3.13).
* **Live Phases 2–7** — `src/ctc/execution/` (settings, models, log, client, state, risk,
  trader, recorder, runner); `decide_live` on real bid/ask; `build_live_estimator()` +
  packaged model artifacts so the bot runs without the raw dataset; read-only testnet probe
  (recorded honestly); feed recording + replay; full execution test suite incl. deterministic
  e2e simulation.

Everything is reproducible with the `ctc-*` CLIs: research (`ctc-validate`, `ctc-eda`,
`ctc-fit`, `ctc-forecast-eval`, `ctc-backtest`, `ctc-build-live`) and live (`ctc-bot`,
`ctc-bot-probe`, `ctc-bot-replay`, `ctc-record`).

## Official pre-event checklist (case packet §7)

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Download `hurricane-season-research.zip`, read README | ✅ Done | 600 seasons extracted under `data/raw/` |
| 2 | Load data with `history.py`, run the reference code | ✅ Done | `ctc-eda` reproduces the 69.6% advisory accuracy exactly |
| 3 | Build a valuation model per company (damage, outages, season losses) | ✅ Done | All 12 symbols; held-out MAE < 1% of start for 10/12 (see below) |
| 4 | Measure official/wire accuracy and how early each identifies the pattern | ✅ Done | `ctc-forecast-eval` → `forecast_eval.json` |
| 5 | Sign in on the testnet, join a team, get a bot key | ⛔ Blocked | Key present but exchange returns `401 Invalid API key`; needs active key (see TESTNET.md) |
| 6 | Connect bot, subscribe to news, place + cancel an order | 🟡 Built | Client+bot implemented & tested offline; place+cancel is the proposed first live test (awaiting key + approval) |
| 7 | Stay connected across season rollovers | 🟡 Built | Reconnect + `on_new_season()` implemented & unit-tested; needs live verification |
| 8 | Record a season + check with `check_recording.py` | 🟡 Built | `ctc-record` writes a check-compatible JSONL; verify once a live key works |
| 9 | Trade a full testnet season by hand and with the bot | ⛔ Blocked | Depends on 5–8 + explicit approval |

**Summary:** all *research/preparation* items (1–4) are complete and verified; the live-bot
stack for items 6–8 is **built and tested offline**, blocked only on a valid bot key (5) and
approval for state-changing actions (9).

## Model performance (held-out, 600-season split)

Held-out **test** settlement MAE (`valuation_metrics.json`), fit on the train split only:

| Symbol | MAE ($) | % of start |   | Symbol | MAE ($) | % of start |
|--------|--------:|-----------:|---|--------|--------:|-----------:|
| PORT   | 0.123 | 0.17% |   | GEN    | 0.338 | 0.84% |
| ISLE   | 0.003 | 0.01% |   | HAVEN  | 0.002 | 0.01% |
| AQUA   | 0.003 | 0.01% |   | REIN   | 0.001 | 0.00% |
| GRID   | 0.201 | 0.55% |   | ARCH   | 0.694 | 0.24% |
| FERRY  | 0.276 | 1.54% |   | REBLD  | 1.888 | 0.87% |
| RECON  | 0.958 | 1.83% |   | MAT    | 0.066 | 0.19% |

Given **realized** outcomes, valuation is essentially solved. The residual risk is in
*forecasting* those outcomes before the damage report (official advisory 69.6% accurate;
track-based damage forecaster MAE 3–7 pts).

## Backtest results (600-season, 360/120/120 split, `backtest_results.json`)

Chronological, leakage-safe replay on 120 held-out test seasons at `half_spread = 0.0015`:

| Metric | Value |
|--------|------:|
| Mean PnL / season | **+1,098.88** |
| Median PnL / season | +1,304.19 |
| Win rate | 61% |
| Std / season | 7,923 |
| 5th-pctile PnL | −12,672 |
| Mean trades / season | 913 |
| Mean spread cost / season | 2,100 |
| Worst drawdown | −33,173 |
| Oracle (perfect-foresight) mean PnL | 23,225 |
| **Oracle capture** | **~5%** |

**PnL attribution by group:** insurers **+118,617** and ETFs **+52,888** carry the book;
island businesses −5,589 and rebuilders **−34,050** are net negative.

**Transaction-cost sensitivity (mean PnL):** 0.0 → +3,469 · 0.001 → +1,796 ·
0.0015 → +1,099 · 0.003 → −701 · 0.005 → −2,598. The edge is real but **thin** and dies
above ~30 bps of half-spread.

### Reading of the results

* The strategy is **net positive and robust in sign**, but captures only ~5% of the
  perfect-foresight oracle and churns heavily (913 trades, $2.1k spread cost/season). It
  is trading too much on weak pre-report signals (priors + the rough damage forecaster).
* **Insurers are the edge.** HAVEN/REIN valuation is near-exact once the season loss is
  known, and the layer structure creates large, predictable repricings at the damage
  report — the strategy already monetizes this.
* **Rebuilders are the leak.** The √-driver models are accurate at settlement but the
  pre-report driver *estimates* (grid outage, repair demand) are noisy, so the strategy
  takes the wrong side early. Gating rebuilder trades to post-report would likely flip
  this group positive.

## Remaining testnet / live integration work

The client, feed→estimator bridge, order manager/rate limiting, reconnect-across-rollover,
recorder, probe, and replay are **implemented and unit-tested** (see `src/ctc/execution/` and
`tests/test_exec_*.py`). What remains is **live verification**, which is blocked on a valid
key and human approval:

1. **Get an active bot key.** Current key 401s; confirm team registration and re-issue the
   key into `.env`. Re-run `ctc-bot-probe` until `auth_time.ok == true` ([TESTNET.md](TESTNET.md)).
2. **First order test (needs approval).** Run the 1-lot PORT place+cancel far from market
   ([TESTNET.md](TESTNET.md)); confirm ack/cancel/parse round-trip.
3. **Dry-run a full season live.** `ctc-bot` against the live feed; confirm reconnect across a
   season rollover and that decisions look sane.
4. **Record + verify.** `ctc-record`, then starter-kit `check_recording.py`.
5. **Confirm live field names.** Tighten the tolerant parsers in `models.py` against the real
   quote/orderbook/ack/snapshot shapes (see "Confirmed vs assumed" in [EXCHANGE.md](EXCHANGE.md)).
6. **Go live (separate approval).** Only after the above: `CTC_DRY_RUN=false` + `ctc-bot --live`.

## Phase 8 — strategy improvements evaluated on held-out seasons

An opt-in refinement was added and evaluated honestly: fit on train (360), **select on
validation (120)**, report on the untouched **test (120)** split. Reproduce with
`ctc-strategy-eval` (writes `research/results/strategy_eval.json`). The baseline
`StrategyConfig` is unchanged and remains the package default.

The improvement is a per-symbol `gate_until_report` filter (hold off trading the configured
symbols until a final damage report arrives) plus a more conservative uncertainty cushion
variant. Held-out **TEST** results (mean PnL / season):

| Variant | Mean PnL | Median | p05 (tail) | Trades | Spread $ | Sharpe-like |
|---------|---------:|-------:|-----------:|-------:|---------:|------------:|
| **baseline** | 1,099 | 1,304 | −12,672 | 913 | 2,100 | 0.14 |
| gate_rebuilders | 1,102 | 1,304 | −12,672 | 909 | 2,092 | 0.14 |
| gate_reb_islands | **1,119** | **1,554** | −13,053 | 875 | 2,007 | 0.14 |
| conservative (k=2.5) | 352 | 887 | **−9,363** | 575 | 1,168 | 0.07 |

**Honest reading:** the gates give only a *marginal* improvement — gating rebuilders alone is
essentially a no-op because the existing **uncertainty gate already suppresses** most
pre-report rebuilder trades (so the earlier "rebuilders are the leak" hypothesis is only
weakly actionable here). Gating rebuilders **and** island businesses nudges mean PnL +1.9%
and median +19% while cutting churn/spread ~4–5%, at a slightly worse tail. The
`conservative` variant roughly **cuts tail risk and churn by ~25–45%** but gives up most of
the mean PnL. No variant dominates, so the **baseline remains the default**; `gate_reb_islands`
is a reasonable low-churn option and the knobs (`gate_until_report`, `uncertainty_k`) are
exposed for live tuning.

The single biggest remaining lever is still the **pre-report damage forecaster** (see below),
not the trade-gating logic.

### Further strategy improvements worth trying (research side)

* Gate rebuilder (and island-business) trades until a damage report arrives; let insurers
  trade on the season-loss estimate earlier.
* Tighten the edge/uncertainty cushion to cut the 913-trade churn and spread cost.
* Improve the pre-report **damage forecaster** (pattern classification from advisories +
  cone, richer track features) — this is the single biggest lever on oracle capture.
* Add the fee-free **ETF create/redeem arbitrage** as a separate, low-risk sleeve.

## Guardrails honored in this handoff

* No merge into `main`; work stays on `feat/research-baseline`.
* No live orders placed; the execution layer is intentionally still a stub.
