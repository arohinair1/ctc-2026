"""Evaluate strategy variants on held-out seasons (keeps the baseline as reference).

Fits valuation/forecaster/priors on the TRAIN split only, selects the best variant on the
VALIDATION split, and reports every variant on the untouched TEST split — so the comparison
is honest (no tuning on test). The baseline ``StrategyConfig`` is always included and left
as the package default; improvements are opt-in configs.

Usage: ``ctc-strategy-eval`` or ``python -m ctc.research.strategy_eval [--n-seasons N]``.
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from ctc.backtest.engine import Backtester
from ctc.config import ISLAND_BUSINESSES, REBUILDERS, RESULTS_DIR
from ctc.data.features import build_feature_table
from ctc.data.history import load
from ctc.data.splits import split_seasons
from ctc.forecasts.damage import DamageForecaster, build_storm_records
from ctc.forecasts.estimator import SeasonEstimator, compute_priors
from ctc.models.valuation import ValuationModel
from ctc.strategy.signals import StrategyConfig


def _variants() -> dict[str, StrategyConfig]:
    """Baseline plus opt-in improvements. Baseline must stay first and unchanged."""
    reb = frozenset(REBUILDERS)
    reb_isl = frozenset(REBUILDERS + ISLAND_BUSINESSES)
    return {
        "baseline": StrategyConfig(),
        "gate_rebuilders": StrategyConfig(gate_until_report=reb),
        "gate_reb_islands": StrategyConfig(gate_until_report=reb_isl),
        "conservative": StrategyConfig(uncertainty_k=2.5),
        "gate_reb_conservative": StrategyConfig(gate_until_report=reb, uncertainty_k=2.5),
    }


def _summary(results) -> dict:
    pnls = np.array([r.pnl for r in results])
    sharpe = float(pnls.mean() / pnls.std()) if pnls.std() > 0 else 0.0
    return {
        "mean_pnl": float(pnls.mean()),
        "median_pnl": float(np.median(pnls)),
        "std_pnl": float(pnls.std()),
        "p05_pnl": float(np.percentile(pnls, 5)),
        "win_rate": float((pnls > 0).mean()),
        "sharpe_like": sharpe,  # mean/std across seasons
        "mean_trades": float(np.mean([r.n_trades for r in results])),
        "mean_spread_cost": float(np.mean([r.spread_cost for r in results])),
        "worst_drawdown": float(min(r.max_drawdown for r in results)),
    }


def _run(cfg, val_model, forecaster, priors, seasons) -> dict:
    bt = Backtester(SeasonEstimator(val_model, forecaster, priors), cfg)
    return _summary([bt.run_season(s) for s in seasons])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-seasons", type=int, default=None, help="limit total seasons loaded")
    parser.add_argument("--select-on", choices=["mean_pnl", "sharpe_like"], default="sharpe_like")
    args = parser.parse_args()

    data = load()
    all_nums = [s.number for s in data.seasons]
    if args.n_seasons:
        all_nums = all_nums[: args.n_seasons]
    split = split_seasons(all_nums)
    print(f"Split: {len(split.train)} train / {len(split.val)} val / {len(split.test)} test")

    df = build_feature_table(data)
    val_model = ValuationModel().fit(df[df["season"].isin(split.train)])
    forecaster = DamageForecaster().fit(build_storm_records(data, split.train))
    priors = compute_priors(data, split.train)

    val_seasons = [s for s in data.seasons if s.number in set(split.val)]
    test_seasons = [s for s in data.seasons if s.number in set(split.test)]

    variants = _variants()
    val_res = {name: _run(cfg, val_model, forecaster, priors, val_seasons) for name, cfg in variants.items()}
    # select the best non-baseline variant on validation, by the chosen metric
    ranked = sorted(val_res.items(), key=lambda kv: kv[1][args.select_on], reverse=True)
    selected = ranked[0][0]
    test_res = {
        name: _run(cfg, val_model, forecaster, priors, test_seasons) for name, cfg in variants.items()
    }

    print(f"\n=== VALIDATION (selection metric: {args.select_on}) ===")
    _print_table(val_res)
    print(f"\nSelected on validation: {selected}")
    print("\n=== TEST (held-out; honest comparison) ===")
    _print_table(test_res)

    base, sel = test_res["baseline"], test_res[selected]
    print(
        f"\nBaseline vs {selected} on TEST: mean_pnl {base['mean_pnl']:,.0f} -> {sel['mean_pnl']:,.0f} "
        f"| trades {base['mean_trades']:,.0f} -> {sel['mean_trades']:,.0f} "
        f"| spread_cost {base['mean_spread_cost']:,.0f} -> {sel['mean_spread_cost']:,.0f}"
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "split": split.as_dict(),
        "select_on": args.select_on,
        "selected": selected,
        "validation": val_res,
        "test": test_res,
    }
    (RESULTS_DIR / "strategy_eval.json").write_text(json.dumps(out, indent=2))
    print(f"\nWrote {RESULTS_DIR / 'strategy_eval.json'}")
    return 0


def _print_table(res: dict) -> None:
    cols = ["mean_pnl", "median_pnl", "p05_pnl", "win_rate", "sharpe_like", "mean_trades", "mean_spread_cost"]
    print(f"  {'variant':22s}" + "".join(f"{c:>14s}" for c in cols))
    for name, s in res.items():
        print(f"  {name:22s}" + "".join(f"{s[c]:>14,.2f}" for c in cols))


if __name__ == "__main__":
    raise SystemExit(main())
