# Research notes — Systematic Equities (Hurricane Season)

Findings from the 600 research seasons (1,800 storms). All numbers are reproducible
via the `ctc-*` commands; see [HANDOFF.md](HANDOFF.md) for how to run them. Results
land in `research/results/` (gitignored).

## 1. Storms and the world

* **Map / sites.** The map is 1000×700 (origin top-left, `y` increases south). Each
  island business owns weighted sites (`case.json`); site weights per company sum to 1.
  Rebuilders and insurers have no physical sites.
* **Pattern frequency** (`eda_summary.json`): direct 25.0%, chain 22.7%, recurve 20.7%,
  stall 15.1%, eyewall 14.5%, erratic 1.9% (as a *starting* pattern). 6.3% of storms
  turn erratic mid-life, on average at age ≈ 247 s.
* **Per-pattern damage signatures** are highly distinctive and are the main lever for
  pre-report valuation:
  * `direct` → PORT (≈39% mean), spares ISLE/AQUA.
  * `stall` → broad, heavy damage (ISLE/AQUA/FERRY all high).
  * `recurve` → AQUA-heavy, PORT ≈ 0.
  * `chain`/`eyewall` → GRID/FERRY and mixed.
* **Season insured loss**: mean $1,009M, p90 $1,422M, max $1,766M (per-storm mean $336M).

## 2. Wind field and outages (`ctc/models/wind.py`)

Calibrated against the 500+ `impact` events (first hurricane conditions on an island):

```
wind(site) = intensity * exp(-distance / radius)
```

At the moment of impact this quantity is nearly constant (coefficient of variation
≈ 10%), far tighter than a linear or Gaussian fall-off. The **closing threshold is
≈ 0.39** (`WIND_CLOSE_THRESHOLD`). A site is "closed" while `wind ≥ threshold`.

Outages are **not** in the feed, so we reconstruct a site-weighted **outage metric**
(weighted closed-seconds) from the 2-second storm observations. Adding this metric to
the valuation lifts FERRY's settlement R² from 0.69 → 0.99 and similarly explains GRID,
confirming the reconstruction.

## 3. Valuation models (`ctc/models/valuation.py`)

Every model maps **realized season outcomes** (cumulative damage, outages, season loss)
to a settlement value. Held-out test MAE (fit on train split, 600-season run,
`valuation_metrics.json`):

| Symbol | MAE ($) | MAE (% of start) | Structure |
|--------|--------:|-----------------:|-----------|
| PORT   | 0.123 | 0.17% | linear in damage + small outage |
| ISLE   | 0.003 | 0.01% | pure damage (R²≈1) |
| AQUA   | 0.003 | 0.01% | pure damage (R²≈1) |
| GRID   | 0.201 | 0.55% | damage + outage |
| FERRY  | 0.276 | 1.54% | damage + outage |
| RECON  | 0.958 | 1.83% | √(PORT+GRID+FERRY damage) + √outage |
| MAT    | 0.066 | 0.19% | √(season_loss) |
| GEN    | 0.338 | 0.84% | √(grid_outage) |
| HAVEN  | 0.002 | 0.01% | insurance layer |
| REIN   | 0.001 | 0.00% | insurance layer |
| ARCH   | 0.694 | 0.24% | basket of constituents |
| REBLD  | 1.888 | 0.87% | basket of constituents |

**Island businesses** settle linearly: `base + b·cumdmg + c·outage` (ISLE/AQUA pure
damage; GRID/FERRY and a little PORT lose value to outages).

**Rebuilders** gain from repair demand with diminishing (√) returns: MAT ~ √(season
loss), GEN ~ √(grid outage), RECON ~ √(port+grid+ferry damage) + √(their outage).

**Insurers** absorb a layer of the season's insured losses (fit by grid search over
attach/width, `fitted_params.json`):

| Insurer | Attach ($M) | Exhaust ($M) | Rate ($/share per $M) |
|---------|------------:|-------------:|----------------------:|
| HAVEN   | 400 | 1,300 | 0.060 |
| REIN    | 1,300 | 1,770 | 0.035 |

This is why HAVEN reacts strongly to mid-size seasons while REIN barely moves until the
season total is extreme — matching Figure 9 in the packet.

**ETFs** settle at exactly their basket value; the models enforce this.

## 4. Forecast / information quality (`ctc/forecasts/advisory.py`, `forecast_eval.json`)

* **Official advisory accuracy** at naming the true (age-aware) pattern: **69.6%**
  overall, roughly flat across storm age (72% in the first minute, ~69% later). This
  reproduces the packet's reference example exactly (10,028 / 14,400).
* **Wire chatter accuracy**: **39.7%** — barely better than the 1-in-6 base rate, and it
  is *right on only 18% of the occasions it disagrees* with the official advisory. Treat
  the wire as weak/contrarian noise; do not trade it on its own.
* **Earliness**: the official advisory durably locks onto the final pattern at a median
  age of ~390 s; it never does so for ~31% of storms (those that turn late/erratic).
* **Per-island hurricane-condition probabilities are well calibrated** (predicted ≈
  observed across all 10 bins), so they can be used directly as outage/closure priors.
* **Track-based damage forecaster** (pre-report only) is a rough baseline: held-out
  damage MAE ≈ 3.4–6.6 percentage points per company; the true-damage → insured-loss map
  is near-exact (MAE ≈ $0.03M). This is the weakest link and the clearest place to
  improve (see HANDOFF.md).

## 5. Leakage-safe inference (`ctc/forecasts/estimator.py`)

The `SeasonEstimator` ingests the feed in time order and, at every instant, estimates the
**final** season outcome using only information available so far:

1. rounds with a **final** damage report → exact values;
2. a round with only a **preliminary** report → the preliminary estimate (+ its measured
   error variance);
3. a round whose storm is being observed but not yet reported → the track-based damage
   forecaster + observed outage so far;
4. a round not yet started → historical **priors** (per-round means/variances).

It also propagates a 1-σ uncertainty per symbol (delta method through each model), which
the strategy uses to gate trading. The integration test
`test_estimator_matches_feature_table_after_full_feed` verifies that, after the whole
feed is ingested, the estimator's cumulative damage/outage/loss match the offline
ground-truth feature table — i.e. no look-ahead and no drift.

## 6. House prices & ETF arbitrage (`backtest_results.json`)

* The house consensus (`market.csv`) is reasonable but slow; its errors are learnable.
* Static ETF mispricing (house ETF price − basket of house prices) is small on average
  (ARCH mean ≈ $0.05, |mean| ≈ $0.84; REBLD mean ≈ −$0.01, |mean| ≈ $0.42) but has fat
  tails (ARCH p95 ≈ $2.3). Create/redeem is fee-free, so this is a clean, low-risk
  intraday signal worth exploiting on the testnet.

## Credits

`ctc/data/history.py` is a packaged, typed evolution of the competition starter kit's
`history.py` (standard-library loader).
