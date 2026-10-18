"""Central configuration: paths, the public case definition and shared constants.

The research dataset (``hurricane-season-research.zip``) is *not* committed to git.
Code resolves the extracted ``history/`` folder from, in order:

1. the ``CTC_DATA_DIR`` environment variable (pointing at the folder that
   contains ``manifest.json``), or
2. the default extraction location under ``data/raw/``.

The public case specification (``case.json``) is small and required for the code
to run, so a copy is committed inside the package at ``ctc/data/case.json``.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repository / package paths
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent.parent
CASE_JSON = PACKAGE_DIR / "data" / "case.json"
FITTED_PARAMS_JSON = PACKAGE_DIR / "models" / "fitted_params.json"
DAMAGE_FORECASTER_JSON = PACKAGE_DIR / "models" / "damage_forecaster.json"
PRIORS_JSON = PACKAGE_DIR / "models" / "priors.json"
RESULTS_DIR = REPO_ROOT / "research" / "results"

# Candidate locations for the extracted research data (folder with manifest.json).
_DATA_CANDIDATES = [
    REPO_ROOT / "data" / "raw" / "hurricane-research" / "hurricane-season-research" / "history",
    REPO_ROOT / "data" / "raw" / "hurricane-season-research" / "history",
    REPO_ROOT / "data" / "raw" / "history",
]


def history_dir() -> Path:
    """Return the ``history/`` folder that contains ``manifest.json``.

    Raises a clear error if the dataset has not been extracted yet.
    """
    env = os.environ.get("CTC_DATA_DIR")
    if env:
        p = Path(env)
        if (p / "manifest.json").exists():
            return p
        if (p / "history" / "manifest.json").exists():
            return p / "history"
        raise FileNotFoundError(f"CTC_DATA_DIR={env} does not contain manifest.json")
    for c in _DATA_CANDIDATES:
        if (c / "manifest.json").exists():
            return c
    raise FileNotFoundError(
        "Could not find the research data. Extract hurricane-season-research.zip "
        "into data/raw/ or set CTC_DATA_DIR to the folder containing manifest.json. "
        f"Looked in: {[str(c) for c in _DATA_CANDIDATES]}"
    )


# ---------------------------------------------------------------------------
# Public case definition
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def load_case() -> dict[str, Any]:
    """The public case specification (map, companies, ETF baskets, limits, timing)."""
    return json.loads(CASE_JSON.read_text())


# Opening/house prices at the first open (from the case packet, section 3).
START_PRICES: dict[str, float] = {
    "PORT": 72.48,
    "ISLE": 50.26,
    "AQUA": 27.33,
    "GRID": 36.41,
    "FERRY": 17.91,
    "RECON": 52.38,
    "MAT": 35.81,
    "GEN": 40.30,
    "HAVEN": 39.55,
    "REIN": 89.36,
    "ARCH": 289.20,
    "REBLD": 216.68,
}

ISLAND_BUSINESSES = ["PORT", "ISLE", "AQUA", "GRID", "FERRY"]
REBUILDERS = ["RECON", "MAT", "GEN"]
INSURERS = ["HAVEN", "REIN"]
STOCKS = ISLAND_BUSINESSES + REBUILDERS + INSURERS
ETFS = ["ARCH", "REBLD"]
ALL_SYMBOLS = STOCKS + ETFS

ISLANDS = ["cascadia_prime", "coral_key", "azure_isle", "palm_atoll"]
OUTLOOKS = ["direct", "chain", "recurve", "stall", "eyewall", "erratic"]

# Wind model calibrated from impact events (see research/eda.py and docs/RESEARCH.md).
# wind(site) = intensity * exp(-distance / radius); a site is "closed" when wind >= threshold.
WIND_CLOSE_THRESHOLD = 0.39


def limits() -> dict[str, int]:
    return load_case()["limits"]


def baskets() -> dict[str, dict[str, int]]:
    return {k: v["basket"] for k, v in load_case()["etfs"].items()}


def company_sites() -> dict[str, list[dict[str, Any]]]:
    return {k: v.get("sites", []) for k, v in load_case()["companies"].items()}
