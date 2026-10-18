"""Build the committed live-inference artifacts from the research data.

The live bot must run without the (gitignored, multi-GB) research dataset, so this script
fits the damage forecaster and the historical priors on *all* research seasons and writes
small JSON artifacts into the package:

* ``src/ctc/models/damage_forecaster.json``
* ``src/ctc/models/priors.json``

(The valuation model's ``fitted_params.json`` is produced by ``ctc-fit``.)

Usage: ``ctc-build-live`` or ``python -m ctc.research.build_live``.
"""

from __future__ import annotations

import argparse

from ctc.config import DAMAGE_FORECASTER_JSON, PRIORS_JSON
from ctc.data.history import load
from ctc.forecasts.damage import DamageForecaster, build_storm_records
from ctc.forecasts.estimator import compute_priors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-seasons", type=int, default=None, help="limit seasons (for a quick build)")
    args = parser.parse_args()

    data = load()
    nums = [s.number for s in data.seasons]
    if args.n_seasons:
        nums = nums[: args.n_seasons]
    print(f"Fitting live artifacts on {len(nums)} research seasons...")

    fc = DamageForecaster().fit(build_storm_records(data, nums))
    fc.save(DAMAGE_FORECASTER_JSON)
    print(f"Wrote {DAMAGE_FORECASTER_JSON}")

    priors = compute_priors(data, nums)
    priors.save(PRIORS_JSON)
    print(f"Wrote {PRIORS_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
