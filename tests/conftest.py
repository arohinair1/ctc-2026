"""Shared pytest fixtures and lightweight synthetic-data factories.

The unit tests must run without the (gitignored, multi-GB) research dataset, so the
helpers here build small, deterministic in-memory objects that *duck-type* the real
:class:`ctc.data.history.Season` / :class:`~ctc.data.history.History` well enough for
the functions under test. Tests that genuinely need the real data use the
``requires_data`` marker/fixture and are skipped when the dataset is absent.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from ctc.config import ISLAND_BUSINESSES, history_dir
from ctc.models.valuation import ValuationModel

# ---------------------------------------------------------------------------
# Synthetic feed-message / storm factories
# ---------------------------------------------------------------------------


def make_storm_obs(
    round: int,
    age: float,
    x: float,
    y: float,
    radius: float = 80.0,
    intensity: float = 1.0,
    seq: int = 0,
) -> dict[str, Any]:
    """A ``storm`` observation feed message."""
    return {
        "type": "storm",
        "round": round,
        "age": age,
        "x": x,
        "y": y,
        "vx": 0.0,
        "vy": -1.8,
        "radius": radius,
        "intensity": intensity,
        "seq": seq,
        "time": f"2025-08-04T10:{int(age) // 60:02d}:{int(age) % 60:02d}",
    }


def make_damage_report(
    round: int,
    damage: dict[str, float],
    loss: float,
    stage: str = "final",
    time: str = "2025-08-04T15:40:00",
    seq: int = 1000,
) -> dict[str, Any]:
    """A ``damage_report`` feed message (final or preliminary)."""
    return {
        "type": "damage_report",
        "round": round,
        "stage": stage,
        "damage_pct": dict(damage),
        "industry_loss_musd": loss,
        "time": time,
        "seq": seq,
    }


def make_advisory(
    round: int,
    outlook: str,
    age: float = 30,
    source: str = "official",
    hurricane_conditions: dict[str, float] | None = None,
    seq: int = 10,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "advisory",
        "round": round,
        "source": source,
        "outlook": outlook,
        "age": age,
        "seq": seq,
        "time": "2025-08-04T10:15:00",
    }
    if hurricane_conditions is not None:
        msg["hurricane_conditions"] = dict(hurricane_conditions)
    return msg


def make_impact(round: int, island: str, age: float = 100, seq: int = 50) -> dict[str, Any]:
    return {
        "type": "impact",
        "round": round,
        "island": island,
        "age": age,
        "seq": seq,
        "time": "2025-08-04T11:02:00",
    }


def make_manifest_storm(
    round: int,
    pattern: str = "direct",
    turned_erratic_at: float | None = None,
    damage: dict[str, float] | None = None,
    loss: float = 100.0,
) -> dict[str, Any]:
    """A manifest storm entry (ground truth)."""
    return {
        "round": round,
        "pattern": pattern,
        "turned_erratic_at": turned_erratic_at,
        "damage_pct": damage or dict.fromkeys(ISLAND_BUSINESSES, 0.0),
        "industry_loss_musd": loss,
        "open": "2025-08-04T09:30:00",
    }


# ---------------------------------------------------------------------------
# Fake Season / History (duck-typed)
# ---------------------------------------------------------------------------


class FakeSeason:
    """Minimal stand-in for :class:`ctc.data.history.Season`."""

    def __init__(
        self,
        number: int,
        storms: list[dict[str, Any]],
        feed: list[dict[str, Any]],
        settlement: dict[str, float] | None = None,
        prices: dict[str, list[float]] | None = None,
        times: list[str] | None = None,
        season_loss_musd: float | None = None,
    ) -> None:
        self.number = number
        self.storms = storms
        self.feed = feed
        self.settlement = settlement or {}
        self.prices = prices or {}
        self.times = times or []
        if season_loss_musd is None:
            season_loss_musd = sum(s["industry_loss_musd"] for s in storms)
        self.season_loss_musd = season_loss_musd

    def messages(self, round: int | None = None, **match: Any) -> list[dict[str, Any]]:
        return [
            m
            for m in self.feed
            if (round is None or m.get("round") == round) and all(m.get(k) == v for k, v in match.items())
        ]

    def storm_of_round(self, round: int) -> dict[str, Any]:
        for s in self.storms:
            if s["round"] == round:
                return s
        raise KeyError(round)

    def track(self, round: int) -> list[tuple[int, float, float, float]]:
        return [(m["age"], m["x"], m["y"], m["radius"]) for m in self.messages(round, type="storm")]


class FakeHistory:
    def __init__(self, seasons: list[FakeSeason]) -> None:
        self.seasons = seasons

    def __len__(self) -> int:
        return len(self.seasons)


# ---------------------------------------------------------------------------
# Synthetic fitted valuation model (data-independent)
# ---------------------------------------------------------------------------


def synthetic_feature_table(n: int = 60, seed: int = 0) -> pd.DataFrame:
    """A season feature table whose settlements are an exact function of the features,
    so fitted models should recover the generating parameters (up to least squares)."""
    rng = np.random.default_rng(seed)
    rows = []
    # ground-truth generating parameters
    island = {
        "PORT": (100.0, -0.55, -0.02),
        "ISLE": (60.0, -0.38, 0.0),
        "AQUA": (35.0, -0.20, 0.0),
        "GRID": (50.0, -0.25, -0.04),
        "FERRY": (25.0, -0.05, -0.06),
    }
    for i in range(n):
        row: dict[str, float] = {"season": i + 1}
        cum = {}
        out = {}
        for sym, (base, bd, co) in island.items():
            cum[sym] = float(rng.uniform(0, 40))
            out[sym] = float(rng.uniform(0, 500))
            row[f"cumdmg_{sym}"] = cum[sym]
            row[f"outage_{sym}"] = out[sym]
            row[f"settle_{sym}"] = base + bd * cum[sym] + co * out[sym]
        loss = float(rng.uniform(400, 1700))
        row["season_loss"] = loss
        row["total_damage"] = sum(cum.values())
        row["recon_damage"] = cum["PORT"] + cum["GRID"] + cum["FERRY"]
        row["recon_outage"] = out["PORT"] + out["GRID"] + out["FERRY"]
        row["grid_outage"] = out["GRID"]
        # insurers: piecewise layers
        row["settle_HAVEN"] = 75.0 - 0.06 * min(max(loss - 400.0, 0.0), 900.0)
        row["settle_REIN"] = 90.0 - 0.035 * min(max(loss - 1300.0, 0.0), 470.0)
        # rebuilders: sqrt drivers
        row["settle_MAT"] = 25.0 + 0.32 * np.sqrt(loss)
        row["settle_GEN"] = 29.0 + 1.2 * np.sqrt(out["GRID"])
        row["settle_RECON"] = 31.0 + 2.1 * np.sqrt(row["recon_damage"]) - 0.16 * np.sqrt(row["recon_outage"])
        # ETFs settle at basket value (consistent)
        row["settle_ARCH"] = (
            row["settle_PORT"]
            + row["settle_ISLE"]
            + 2 * row["settle_AQUA"]
            + row["settle_GRID"]
            + 2 * row["settle_FERRY"]
            + row["settle_HAVEN"]
        )
        row["settle_REBLD"] = 2 * row["settle_RECON"] + 2 * row["settle_MAT"] + row["settle_GEN"]
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture(scope="session")
def synth_table() -> pd.DataFrame:
    return synthetic_feature_table()


@pytest.fixture(scope="session")
def synth_model(synth_table: pd.DataFrame) -> ValuationModel:
    return ValuationModel().fit(synth_table)


# ---------------------------------------------------------------------------
# Real-data gating
# ---------------------------------------------------------------------------


def _data_available() -> bool:
    try:
        history_dir()
        return True
    except FileNotFoundError:
        return False


requires_data = pytest.mark.skipif(
    not _data_available(), reason="research dataset not available (set CTC_DATA_DIR)"
)
