"""Evaluate storm forecasting and information quality (Phase 4 deliverables).

Measures advisory/wire accuracy (overall and by storm age), earliness, confusion
matrices, hurricane-condition calibration, the marginal value of the wire, and the
held-out accuracy of the track-based damage forecaster. Writes
``research/results/forecast_eval.json``. Run with ``ctc-forecast-eval``.
"""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")

import numpy as np  # noqa: E402

from ctc.config import ISLAND_BUSINESSES, RESULTS_DIR  # noqa: E402
from ctc.data.history import load  # noqa: E402
from ctc.data.splits import split_seasons  # noqa: E402
from ctc.forecasts.advisory import (  # noqa: E402
    accuracy,
    confusion_matrix,
    earliness,
    hurricane_condition_calibration,
    wire_adds_information,
)
from ctc.forecasts.damage import DamageForecaster, build_storm_records  # noqa: E402


def damage_forecaster_eval(history, split) -> dict:
    fc = DamageForecaster().fit(build_storm_records(history, split.train))
    test = build_storm_records(history, split.test)
    errs = {sym: [] for sym in ISLAND_BUSINESSES}
    loss_err = []
    for rec in test:
        pred = fc.predict_damage(rec["features"])
        for sym in ISLAND_BUSINESSES:
            errs[sym].append(pred[sym] - rec["damage"][sym])
        loss_err.append(fc.predict_loss(rec["damage"]) - rec["loss"])
    out = {
        sym: {
            "mae": float(np.mean(np.abs(errs[sym]))),
            "rmse": float(np.sqrt(np.mean(np.square(errs[sym])))),
        }
        for sym in ISLAND_BUSINESSES
    }
    out["loss"] = {
        "mae": float(np.mean(np.abs(loss_err))),
        "rmse": float(np.sqrt(np.mean(np.square(loss_err)))),
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()  # noqa: F841

    history = load()
    split = split_seasons([s.number for s in history.seasons])

    acc = accuracy(history)
    early = earliness(history)
    cal = hurricane_condition_calibration(history)
    wire = wire_adds_information(history)
    conf_off = confusion_matrix(history, "official")
    conf_wire = confusion_matrix(history, "wire")
    fc_eval = damage_forecaster_eval(history, split)

    result = {
        "accuracy_overall": acc.overall,
        "accuracy_by_age": {
            src: {f"{lo}-{hi}": v for (lo, hi), v in d.items()} for src, d in acc.by_age.items()
        },
        "earliness": early,
        "calibration": {
            "predicted": cal.predicted,
            "observed": cal.observed,
            "counts": cal.counts,
        },
        "wire_value": wire,
        "confusion_official": conf_off,
        "confusion_wire": conf_wire,
        "damage_forecaster": fc_eval,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "forecast_eval.json").write_text(json.dumps(result, indent=2))

    print("Advisory accuracy (overall):", {k: round(v, 3) for k, v in acc.overall.items()})
    print("Accuracy by age (official):", {k: round(v, 2) for k, v in acc.by_age.get("official", {}).items()})
    print(
        "Earliness (official):",
        {k: round(v, 1) if isinstance(v, float) else v for k, v in early["official"].items()},
    )
    print("Wire value:", {k: round(v, 3) if isinstance(v, float) else v for k, v in wire.items()})
    print(
        "Damage forecaster held-out MAE:",
        {s: round(fc_eval[s]["mae"], 2) for s in ISLAND_BUSINESSES},
        "loss MAE",
        round(fc_eval["loss"]["mae"], 2),
    )
    print(f"Wrote {RESULTS_DIR / 'forecast_eval.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
