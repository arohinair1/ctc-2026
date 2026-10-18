"""Run the chronological backtest over held-out seasons and summarize performance.

Fits the valuation model, damage forecaster and priors on the TRAIN split only, then
backtests on the TEST split (whole-season separation => no look-ahead). Reports total
and per-season PnL, drawdowns, per-symbol and per-group attribution, turnover, a
transaction-cost sweep, and a comparison against simple baselines.

Usage: ``ctc-backtest`` or ``python -m ctc.research.run_backtest [--n-seasons N] [--test N]``.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np

from ctc.backtest.engine import Backtester
from ctc.config import (
    ALL_SYMBOLS,
    ETFS,
    INSURERS,
    ISLAND_BUSINESSES,
    REBUILDERS,
    RESULTS_DIR,
    baskets,
)
from ctc.data.history import load
from ctc.data.splits import split_seasons
from ctc.forecasts.damage import DamageForecaster, build_storm_records
from ctc.forecasts.estimator import SeasonEstimator, compute_priors
from ctc.models.valuation import ValuationModel
from ctc.strategy.signals import StrategyConfig

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")

GROUPS = {
    "island_business": ISLAND_BUSINESSES,
    "rebuilder": REBUILDERS,
    "insurer": INSURERS,
    "etf": ETFS,
}


def _summary(results) -> dict:
    pnls = np.array([r.pnl for r in results])
    return {
        "n_seasons": len(results),
        "total_pnl": float(pnls.sum()),
        "mean_pnl": float(pnls.mean()),
        "median_pnl": float(np.median(pnls)),
        "std_pnl": float(pnls.std()),
        "min_pnl": float(pnls.min()),
        "max_pnl": float(pnls.max()),
        "p05_pnl": float(np.percentile(pnls, 5)),
        "win_rate": float((pnls > 0).mean()),
        "mean_trades": float(np.mean([r.n_trades for r in results])),
        "mean_turnover": float(np.mean([r.turnover for r in results])),
        "mean_spread_cost": float(np.mean([r.spread_cost for r in results])),
        "mean_max_drawdown": float(np.mean([r.max_drawdown for r in results])),
        "worst_drawdown": float(min(r.max_drawdown for r in results)),
    }


def _group_attrib(results) -> dict:
    by_sym = dict.fromkeys(ALL_SYMBOLS, 0.0)
    for r in results:
        for s, v in r.pnl_by_symbol.items():
            by_sym[s] += v
    by_group = {g: float(sum(by_sym[s] for s in syms)) for g, syms in GROUPS.items()}
    return {"by_symbol": {k: float(v) for k, v in by_sym.items()}, "by_group": by_group}


def etf_mispricing_report(history, test_nums) -> dict:
    """Distribution of ETF house price minus basket-of-house-prices (static arb signal)."""
    out = {}
    for etf, basket in baskets().items():
        diffs = []
        for s in history.seasons:
            if s.number not in set(test_nums):
                continue
            prices = s.prices
            n = len(prices[etf])
            for i in range(0, n, 20):  # sample every ~40s
                basket_val = sum(prices[sym][i] * q for sym, q in basket.items())
                diffs.append(prices[etf][i] - basket_val)
        a = np.array(diffs)
        out[etf] = {
            "mean_premium": float(a.mean()),
            "std_premium": float(a.std()),
            "p05": float(np.percentile(a, 5)),
            "p95": float(np.percentile(a, 95)),
            "abs_mean": float(np.abs(a).mean()),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-seasons", type=int, default=None, help="limit total seasons loaded")
    parser.add_argument("--test", type=int, default=None, help="limit number of test seasons run")
    parser.add_argument("--half-spread", type=float, default=0.0015)
    args = parser.parse_args()

    data = load()
    all_nums = [s.number for s in data.seasons]
    if args.n_seasons:
        all_nums = all_nums[: args.n_seasons]
    split = split_seasons(all_nums)
    print(f"Split: {len(split.train)} train / {len(split.val)} val / {len(split.test)} test")

    val_model = ValuationModel().fit(_table(data, split.train))
    forecaster = DamageForecaster().fit(build_storm_records(data, split.train))
    priors = compute_priors(data, split.train)

    cfg = StrategyConfig(half_spread=args.half_spread)
    estimator = SeasonEstimator(val_model, forecaster, priors)
    bt = Backtester(estimator, cfg)

    test_nums = split.test[: args.test] if args.test else split.test
    seasons = [s for s in data.seasons if s.number in set(test_nums)]
    print(f"Backtesting {len(seasons)} test seasons (half_spread={args.half_spread})...")
    results = [bt.run_season(s) for s in seasons]

    summary = _summary(results)
    attrib = _group_attrib(results)

    # Baseline: oracle fair value = actual settlement (upper bound under same costs/limits)
    oracle = _run_oracle(data, seasons, cfg)

    # Transaction-cost sweep
    sweep = {}
    for hs in (0.0, 0.001, 0.0015, 0.003, 0.005):
        c = StrategyConfig(half_spread=hs)
        bt2 = Backtester(SeasonEstimator(val_model, forecaster, priors), c)
        rs = [bt2.run_season(s) for s in seasons]
        sweep[f"half_spread_{hs}"] = _summary(rs)["mean_pnl"]

    etf_rep = etf_mispricing_report(data, test_nums)

    print("\n=== Strategy (model fair value) ===")
    for k, v in summary.items():
        print(f"  {k:20s} {v:,.2f}")
    print("\n=== PnL by group ===")
    for g, v in attrib["by_group"].items():
        print(f"  {g:16s} {v:,.1f}")
    print("\n=== Oracle upper bound (fair=settlement) mean PnL ===")
    print(
        f"  {oracle['mean_pnl']:,.2f}  (strategy captures "
        f"{100 * summary['mean_pnl'] / oracle['mean_pnl']:.0f}% of oracle)"
    )
    print("\n=== Transaction-cost sensitivity (mean PnL) ===")
    for k, v in sweep.items():
        print(f"  {k:22s} {v:,.2f}")
    print("\n=== ETF static mispricing (house ETF - basket) ===")
    for etf, r in etf_rep.items():
        print(f"  {etf}: mean={r['mean_premium']:+.2f} std={r['std_premium']:.2f} |mean|={r['abs_mean']:.2f}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "backtest_results.json").write_text(
        json.dumps(
            {
                "config": {"half_spread": args.half_spread},
                "split": split.as_dict(),
                "summary": summary,
                "attribution": attrib,
                "oracle": oracle,
                "cost_sweep": sweep,
                "etf_mispricing": etf_rep,
                "per_season_pnl": {r.season: r.pnl for r in results},
            },
            indent=2,
        )
    )
    print(f"\nWrote {RESULTS_DIR / 'backtest_results.json'}")
    return 0


def _table(data, nums):
    from ctc.data.features import build_feature_table

    df = build_feature_table(data)
    return df[df["season"].isin(nums)]


def _run_oracle(data, seasons, cfg) -> dict:
    """Upper bound: a strategy that knows the final settlement from t=0."""
    from ctc.backtest.accounting import Portfolio
    from ctc.strategy.signals import decide

    results = []
    for s in seasons:
        pf = Portfolio()
        fair = s.settlement
        for i in range(len(s.times)):
            mids = {sym: s.prices[sym][i] for sym in ALL_SYMBOLS}
            for d in decide(fair, mids, pf.positions, cfg):
                if d.order:
                    pf.fill(d.symbol, d.order, d.executable, mids[d.symbol])
        results.append(pf.settle(s.settlement))
    arr = np.array(results)
    return {"mean_pnl": float(arr.mean()), "total_pnl": float(arr.sum())}


if __name__ == "__main__":
    raise SystemExit(main())
