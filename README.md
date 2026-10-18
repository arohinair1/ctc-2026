# CTC 2026 — Systematic Equities (Hurricane Season)

Research and trading baseline for the Cornell Trading Competition 2026 Systematic
Equities case. The Archipelago Exchange lists 10 stocks + 2 ETFs whose fundamental values
are driven by hurricane damage, site outages and insured losses over a three-round
season. This repo finds those values before the market does and trades on them.

- **Research findings:** [docs/RESEARCH.md](docs/RESEARCH.md)
- **Status, checklist, performance & remaining work:** [docs/HANDOFF.md](docs/HANDOFF.md)
- **Case packets:** `docs/*.pdf`

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add "live" extra for the (future) websocket client

# Extract hurricane-season-research.zip into data/raw/ (not committed), or point to it:
export CTC_DATA_DIR=/path/to/history    # folder containing manifest.json

pytest -q                         # 75 tests (data-backed ones auto-skip without the data)
```

## Reproduce the research

Each command writes to `research/results/` (gitignored):

| Command | What it does |
|---------|--------------|
| `ctc-validate` | Validate the 600-season dataset (feed integrity, settlements, consistency) |
| `ctc-eda` | Exploratory analysis → `eda_summary.json` (+ figures) |
| `ctc-fit` | Fit valuation models, write `src/ctc/models/fitted_params.json` + held-out metrics |
| `ctc-forecast-eval` | Advisory/wire accuracy, calibration, damage-forecaster eval |
| `ctc-backtest` | Chronological leakage-safe backtest on held-out seasons |

## Layout

```
src/ctc/
  config.py            paths, case.json, symbols, limits, constants
  data/                loader (history), validator, feature builder, season splits
  models/              wind/outage field, fundamental valuation (+ fitted_params.json)
  forecasts/           damage forecaster, advisory analysis, leakage-safe estimator
  strategy/            baseline edge-vs-cost decision logic
  risk/                position/order-limit enforcement
  backtest/            event-driven engine + portfolio accounting
  research/            CLI entry points (eda / fit / forecast-eval / run_backtest)
  execution/           live exchange client — STUB, see docs/HANDOFF.md
tests/                 pytest suite (unit + data-gated integration)
```

## Design principles

- **No look-ahead.** Splits are by whole season; the `SeasonEstimator` only uses feed
  information available at each instant. Verified by tests.
- **Interpretable models.** Linear / piecewise-linear / √-diminishing-returns, each fit
  with held-out evaluation — easy for the team to audit and extend.
- **Explicit execution assumptions.** The research data has no order book, so fills are
  simulated at `mid ± half_spread`; read backtest PnL as edge-capture under that
  assumption and confirm on the testnet.

## Headline results (held-out, 600-season split)

- Valuation held-out MAE < 1% of start price for 10 of 12 symbols.
- Official advisories name the true storm pattern 69.6% of the time; wire chatter 39.7%.
- Backtest: **+1,099 mean PnL/season, 61% win rate**; insurers and ETFs drive profit,
  rebuilders are the current weak spot. See [docs/HANDOFF.md](docs/HANDOFF.md).

> Guardrails: development happens on `feat/research-baseline`; the live execution layer is
> a stub and places no orders.
