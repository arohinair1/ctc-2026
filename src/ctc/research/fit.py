"""Fit the valuation models on a training split and evaluate on held-out seasons.

Writes:
* ``src/ctc/models/fitted_params.json`` -- the fitted parameters used at run time,
* ``research/results/valuation_metrics.json`` -- held-out MAE/RMSE per symbol,
* ``research/results/feature_table.csv`` -- the per-season feature table (gitignored).

Usage: ``ctc-fit`` or ``python -m ctc.research.fit [--n-seasons N]``.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np

from ctc.config import (
    ALL_SYMBOLS,
    ETFS,
    FITTED_PARAMS_JSON,
    RESULTS_DIR,
    START_PRICES,
)
from ctc.data.features import build_feature_table
from ctc.data.history import load
from ctc.data.splits import split_seasons
from ctc.models.valuation import ValuationModel

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")


def _metrics(model: ValuationModel, table) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for _, row in table.iterrows():
        feats = {k: row[k] for k in row.index if k.startswith(("cumdmg_", "outage_"))}
        feats["season_loss"] = row["season_loss"]
        pred = model.predict_all(feats)
        for sym in ALL_SYMBOLS:
            out.setdefault(sym, {"err2": 0.0, "abs": 0.0, "n": 0})
            e = pred[sym] - row[f"settle_{sym}"]
            out[sym]["err2"] += e * e
            out[sym]["abs"] += abs(e)
            out[sym]["n"] += 1
    summary = {}
    for sym, d in out.items():
        n = d["n"]
        summary[sym] = {
            "mae": d["abs"] / n,
            "rmse": (d["err2"] / n) ** 0.5,
            "start_price": START_PRICES[sym],
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-seasons", type=int, default=None, help="limit number of seasons")
    args = parser.parse_args()

    data = load()
    print(f"Loaded {len(data)} seasons. Building feature table...")
    table = build_feature_table(data, n_seasons=args.n_seasons)

    nums = table["season"].tolist()
    split = split_seasons(nums)
    tr = table[table["season"].isin(split.train)]
    va = table[table["season"].isin(split.val)]
    te = table[table["season"].isin(split.test)]
    print(f"Split: {len(tr)} train / {len(va)} val / {len(te)} test seasons")

    model = ValuationModel().fit(tr)
    model.save(FITTED_PARAMS_JSON)
    print(f"Saved fitted params -> {FITTED_PARAMS_JSON}")

    test_metrics = _metrics(model, te)
    val_metrics = _metrics(model, va)

    print("\nHeld-out TEST metrics (settlement $):")
    print(f"{'sym':6s} {'MAE':>8s} {'RMSE':>8s} {'MAE%start':>10s}")
    for sym in ALL_SYMBOLS:
        m = test_metrics[sym]
        pct = 100 * m["mae"] / m["start_price"]
        print(f"{sym:6s} {m['mae']:8.3f} {m['rmse']:8.3f} {pct:9.2f}%")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "valuation_metrics.json").write_text(
        json.dumps({"test": test_metrics, "val": val_metrics, "split": split.as_dict()}, indent=2)
    )
    table.to_csv(RESULTS_DIR / "feature_table.csv", index=False)

    # Insurance layer summary
    print("\nFitted insurance layers:")
    for sym, m in model.insurers.items():
        print(
            f"  {sym}: base={m.base:.2f} attach={m.attach:.0f} exhaust={m.attach + m.width:.0f} "
            f"rate={-m.slope:.4f} $/share per $M"
        )
    etf_mae = np.mean([test_metrics[e]["mae"] for e in ETFS])
    print(f"\nETF mean held-out MAE: {etf_mae:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
