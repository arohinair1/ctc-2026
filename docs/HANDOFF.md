# Handoff — CTC 2026 Systematic Equities

Status of the research/trading baseline on branch `feat/research-baseline`, and what is
left before and during the event. Companion to [RESEARCH.md](RESEARCH.md).

## What is done (Phases 1–7)

* **Phase 1–3** — project scaffolding, typed data loader/validator, wind + outage model,
  fundamental valuation models for all 12 symbols.
* **Phase 4–5** — forecast/information-quality analysis, leakage-safe `SeasonEstimator`,
  event-driven backtester + baseline strategy + risk limits.
* **Phase 6** — automated test suite (75 tests) + GitHub Actions CI (lint + format +
  pytest on Python 3.10–3.13).
* **Phase 7** — this handoff + research write-up.

Everything is reproducible with the `ctc-*` CLIs (`ctc-validate`, `ctc-eda`, `ctc-fit`,
`ctc-forecast-eval`, `ctc-backtest`).

## Official pre-event checklist (case packet §7)

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Download `hurricane-season-research.zip`, read README | ✅ Done | 600 seasons extracted under `data/raw/` |
| 2 | Load data with `history.py`, run the reference code | ✅ Done | `ctc-eda` reproduces the 69.6% advisory accuracy exactly |
| 3 | Build a valuation model per company (damage, outages, season losses) | ✅ Done | All 12 symbols; held-out MAE < 1% of start for 10/12 (see below) |
| 4 | Measure official/wire accuracy and how early each identifies the pattern | ✅ Done | `ctc-forecast-eval` → `forecast_eval.json` |
| 5 | Sign in on the testnet, join a team, get a bot key | ⛔ Pending | Testnet not wired up; needs captain-issued bot key |
| 6 | Connect bot, subscribe to news, place + cancel an order | ⛔ Pending | `ctc/execution/` is a stub (see below) |
| 7 | Stay connected across season rollovers | ⛔ Pending | Requires the live client |
| 8 | Record a season (`record_feed.py`) + check (`check_recording.py`) | ⛔ Pending | Starter-kit tools are available under `data/raw/.../starter_kit/` |
| 9 | Trade a full testnet season by hand and with the bot | ⛔ Pending | Depends on 5–8 |

**Summary:** all *research/preparation* items (1–4) are complete and verified; all
*testnet/live-bot* items (5–9) remain and are the critical pre-event path.

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

Ordered by priority for the event:

1. **Live exchange client** (`ctc/execution/`, currently empty). Wrap the REST API
   (`POST/DELETE /orders`, `GET /positions`, `/orderbook`, ETF create/redeem) and the
   `/ws/v1/market-data` WebSocket. Send the bot key in `X-API-Key` and a custom
   `User-Agent` (the default urllib UA is refused). The `websockets` extra is already
   declared (`pip install -e ".[live]"`).
2. **Feed → estimator bridge.** Normalize live feed frames to the same dict shape the
   `SeasonEstimator` already ingests (they match the research `feed.jsonl` schema), using
   `seq` for dedup and snapshots for late-join/catch-up.
3. **Order manager / rate limiting.** Respect 60 actions/min per bot key (180/team),
   200 resting orders/symbol, and the position limits already enforced in
   `ctc/risk/limits.py`. Reuse `clamp_order` for live sizing.
4. **Reconnect across season rollover.** Positions reset to zero and the competition id
   changes at each new season; the client must re-subscribe and the estimator must
   `reset()` per season.
5. **Recording verification.** Run `starter_kit/record_feed.py` then
   `check_recording.py`; confirm it prints "recording matches the published feed" before
   trusting the live parser.
6. **Dry run.** Connect, subscribe to `news`, place one order and cancel it; then trade a
   full testnet season by hand and with the bot.

### Strategy improvements worth trying (research side)

* Gate rebuilder (and island-business) trades until a damage report arrives; let insurers
  trade on the season-loss estimate earlier.
* Tighten the edge/uncertainty cushion to cut the 913-trade churn and spread cost.
* Improve the pre-report **damage forecaster** (pattern classification from advisories +
  cone, richer track features) — this is the single biggest lever on oracle capture.
* Add the fee-free **ETF create/redeem arbitrage** as a separate, low-risk sleeve.

## Guardrails honored in this handoff

* No merge into `main`; work stays on `feat/research-baseline`.
* No live orders placed; the execution layer is intentionally still a stub.
