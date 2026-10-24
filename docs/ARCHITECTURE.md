# Architecture

End-to-end data flow and the module map for the CTC 2026 bot. The design separates
**research** (leakage-safe valuation/forecasting, fit offline) from **execution** (live I/O,
risk, order management), so the live bot reuses the exact same models that were validated in
backtest.

## Pipeline

```
          live WS feed / recording / research feed.jsonl
                              │
                    ┌─────────▼──────────┐
                    │  ExchangeClient    │  REST + WebSocket (async, reconnect, rate-limit)
                    │  (execution/client)│  injectable transport + ws_connect for tests
                    └─────────┬──────────┘
                      Frame objects (quote / orderbook / news / _reconnect)
                              │
                    ┌─────────▼──────────┐
                    │     LiveState      │  quotes, books, positions, open orders
                    │  (execution/state) │  seq dedup, season rollover, snapshot resync
                    └─────────┬──────────┘
          feeds news/damage → │  → SeasonEstimator (forecasts/estimator)  [leakage-safe]
                              │        fair_values_with_uncertainty()
                    ┌─────────▼──────────┐
                    │    LiveTrader      │  propose() = pure decision stage
                    │ (execution/trader) │  execute() = gated I/O stage
                    └─────────┬──────────┘
                  decide_live (strategy/signals) on REAL bid/ask
                              │ candidate OrderRequests
                    ┌─────────▼──────────┐
                    │    RiskManager     │  kill-switch, stale, position & resting limits,
                    │  (execution/risk)  │  per-order cap, gross exposure, dedup
                    └─────────┬──────────┘
                     approved OrderRequests
                              │
                 dry_run / kill_switch gate ── yes ──► log "DRY-RUN would submit", stop
                              │ no
                              ▼
                    ExchangeClient.place_order  (rate-limited)
```

## Module map

### `src/ctc/execution/` (live, new)

| Module | Responsibility |
|--------|----------------|
| `settings.py` | `LiveConfig` + `.env` loader; dry-run default; redaction; risk params |
| `models.py` | Typed protocol objects (Quote, OrderBook, OrderRequest, OrderAck, OpenOrder, Position, Frame) + tolerant parsers |
| `log.py` | Logging setup + `redact()` (keeps the key out of logs) |
| `client.py` | `ExchangeClient` (async REST+WS), `RateLimiter`, `ExchangeError`, read-only `probe()` |
| `state.py` | `LiveState`: market/season state, seq dedup, season rollover, snapshot resync, feeds the estimator |
| `risk.py` | `RiskManager`: pure, synchronous pre-trade checks |
| `trader.py` | `LiveTrader`: orchestrates state→forecast→strategy→risk→execution; dry-run/kill-switch gate |
| `recorder.py` | Feed recording (JSONL) + `iter_recorded_frames` replay + `recording_gaps` |
| `runner.py` | Entry points: `ctc-bot`, `ctc-bot-probe`, `ctc-bot-replay` |

### `src/ctc/` (research, reused by the live bot)

| Module | Responsibility |
|--------|----------------|
| `config.py` | Symbols, start prices, paths, position limits source |
| `forecasts/estimator.py` | `SeasonEstimator` (leakage-safe), `build_live_estimator()` factory, `Priors` |
| `strategy/signals.py` | `decide()` (backtest, synthetic spread) + `decide_live()` (real bid/ask) |
| `risk/limits.py` | `position_limit`, `clamp_order` (shared by backtest + live) |
| `valuation/`, `forecasts/` | Fundamental models + damage forecaster |
| `research/` | EDA, fitting, backtester, `build_live.py` (fits packaged artifacts) |

## Key design decisions

* **Models are fit offline, loaded live.** `build_live_estimator()` loads packaged
  `fitted_params.json` (+ optional `damage_forecaster.json`, `priors.json`), with graceful
  degradation if artifacts are missing, so the bot runs without the raw 600-season dataset.
* **Decision stage is pure.** `LiveTrader.propose()` does no I/O and returns a `ProposalLog`
  (approved + rejected with reasons), making it fully unit-testable and safe to inspect in
  dry-run.
* **Single execution chokepoint.** Only `LiveTrader.execute()` touches write endpoints, and
  it is hard-gated by `dry_run`/`kill_switch`.
* **Testability.** HTTP goes through an injectable sync `transport`; the WS through an
  injectable `ws_connect`. Tests pass fakes and never hit the network (see
  `tests/test_exec_*.py`).
* **Leakage safety carries over.** The same `SeasonEstimator` used in the backtest consumes
  the live feed, so live valuations match validated behavior.

## Live inputs and update cadence

The runner refreshes competition status every five seconds and recalculates decisions every
five seconds, using the latest accepted storm observations/damage reports and executable
bid/ask quotes. Its WebSocket subscription also requests `trades` and `orderbook`; these
frames are retained/recorded for inspection, but the baseline signal model does not yet use
trade flow or depth to change fair values or sizing. Advisory messages are not currently a
feature of the baseline estimator. The quote-aware model added under
`research/quote_aware/` is a separate research candidate, not loaded by `ctc-bot`.

This is adaptive decision-making from new inputs, not online model retraining or code
self-modification. Promote the candidate only after both strategies are evaluated on the
same seasons, fills, costs, sizing and settlement assumptions.
