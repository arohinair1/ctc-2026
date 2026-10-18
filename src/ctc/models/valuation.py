"""Fundamental valuation models for all 12 symbols.

Every model maps *realized season outcomes* (cumulative damage, site outages and
total insured losses) to the settlement value of a symbol. They are intentionally
interpretable (linear, piecewise-linear, square-root diminishing returns) and are
fit on historical seasons with held-out evaluation (see :mod:`ctc.research.fit`).

Empirical structure discovered from the 600 research seasons (docs/RESEARCH.md):

* Island businesses settle linearly in cumulative damage; GRID/FERRY (and a little
  PORT) also lose value to site outages. ISLE/AQUA are pure-damage (R^2 = 1.0).
* Rebuilders rise with repair demand but with diminishing returns (square root):
  MAT ~ sqrt(season_loss), GEN ~ sqrt(grid_outage),
  RECON ~ sqrt(port+grid+ferry damage) + sqrt(their outage).
* Insurers pay a layer of the season's insured losses: HAVEN ~ [400, 1300] $M at
  0.06 $/share per $M; REIN ~ [1300, 1660] $M at 0.035 $/share per $M.
* ETFs are worth exactly their constituent baskets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ctc.config import (
    ETFS,
    INSURERS,
    ISLAND_BUSINESSES,
    REBUILDERS,
    baskets,
)

# ---------------------------------------------------------------------------
# Feature vector used for prediction
# ---------------------------------------------------------------------------
# A "features" dict for a season has keys:
#   cumdmg_<SYM>, outage_<SYM> for the five island businesses,
#   season_loss
# Derived features are computed internally.


def _derive(features: dict[str, float]) -> dict[str, float]:
    f = dict(features)
    f["total_damage"] = sum(f.get(f"cumdmg_{s}", 0.0) for s in ISLAND_BUSINESSES)
    f["recon_damage"] = sum(f.get(f"cumdmg_{s}", 0.0) for s in ("PORT", "GRID", "FERRY"))
    f["recon_outage"] = sum(f.get(f"outage_{s}", 0.0) for s in ("PORT", "GRID", "FERRY"))
    f["grid_outage"] = f.get("outage_GRID", 0.0)
    return f


# ---------------------------------------------------------------------------
# Island businesses: settle = base + b*cumdmg + c*outage
# ---------------------------------------------------------------------------
@dataclass
class IslandBusinessModel:
    base: float = 0.0
    b_damage: float = 0.0
    c_outage: float = 0.0

    def fit(self, cumdmg: np.ndarray, outage: np.ndarray, settle: np.ndarray) -> IslandBusinessModel:
        A = np.column_stack([cumdmg, outage, np.ones_like(cumdmg)])
        coef, *_ = np.linalg.lstsq(A, settle, rcond=None)
        self.b_damage, self.c_outage, self.base = float(coef[0]), float(coef[1]), float(coef[2])
        return self

    def predict(self, cumdmg: float, outage: float) -> float:
        return self.base + self.b_damage * cumdmg + self.c_outage * outage


# ---------------------------------------------------------------------------
# Insurers: settle = base + slope * clip(loss - attach, 0, width)
# ---------------------------------------------------------------------------
@dataclass
class InsurerModel:
    base: float = 0.0
    slope: float = 0.0
    attach: float = 0.0
    width: float = 0.0

    def fit(self, loss: np.ndarray, settle: np.ndarray) -> InsurerModel:
        best = None
        for attach in np.linspace(200, 1500, 131):
            for width in np.linspace(100, 1300, 121):
                pay = np.clip(loss - attach, 0.0, width)
                A = np.column_stack([pay, np.ones_like(pay)])
                coef, *_ = np.linalg.lstsq(A, settle, rcond=None)
                pred = A @ coef
                mae = float(np.mean(np.abs(pred - settle)))
                if best is None or mae < best[0]:
                    best = (mae, float(attach), float(width), float(coef[1]), float(coef[0]))
        _, self.attach, self.width, self.base, self.slope = best
        return self

    def payout(self, loss: float) -> float:
        """Per-share loss absorbed in this insurer's layer (positive number)."""
        return -self.slope * min(max(loss - self.attach, 0.0), self.width)

    def predict(self, loss: float) -> float:
        return self.base + self.slope * min(max(loss - self.attach, 0.0), self.width)


# ---------------------------------------------------------------------------
# Rebuilders: settle = base + sum_k coef_k * sqrt(driver_k)
# ---------------------------------------------------------------------------
@dataclass
class RebuilderModel:
    drivers: list[str] = field(default_factory=list)  # feature keys, used under sqrt
    base: float = 0.0
    coefs: list[float] = field(default_factory=list)

    def _design(self, table: pd.DataFrame) -> np.ndarray:
        cols = [np.sqrt(np.clip(table[d].to_numpy(dtype=float), 0, None)) for d in self.drivers]
        return np.column_stack(cols + [np.ones(len(table))])

    def fit(self, table: pd.DataFrame, settle: np.ndarray) -> RebuilderModel:
        A = self._design(table)
        coef, *_ = np.linalg.lstsq(A, settle, rcond=None)
        self.coefs = [float(c) for c in coef[:-1]]
        self.base = float(coef[-1])
        return self

    def predict(self, features: dict[str, float]) -> float:
        val = self.base
        for d, c in zip(self.drivers, self.coefs, strict=False):
            val += c * np.sqrt(max(features.get(d, 0.0), 0.0))
        return float(val)


# ---------------------------------------------------------------------------
# Unified valuation model
# ---------------------------------------------------------------------------
REBUILDER_DRIVERS = {
    "MAT": ["season_loss"],
    "GEN": ["grid_outage"],
    "RECON": ["recon_damage", "recon_outage"],
}


@dataclass
class ValuationModel:
    islands: dict[str, IslandBusinessModel] = field(default_factory=dict)
    insurers: dict[str, InsurerModel] = field(default_factory=dict)
    rebuilders: dict[str, RebuilderModel] = field(default_factory=dict)

    # ---- fitting -------------------------------------------------------
    def fit(self, table: pd.DataFrame) -> ValuationModel:
        for sym in ISLAND_BUSINESSES:
            self.islands[sym] = IslandBusinessModel().fit(
                table[f"cumdmg_{sym}"].to_numpy(float),
                table[f"outage_{sym}"].to_numpy(float),
                table[f"settle_{sym}"].to_numpy(float),
            )
        for sym in INSURERS:
            self.insurers[sym] = InsurerModel().fit(
                table["season_loss"].to_numpy(float),
                table[f"settle_{sym}"].to_numpy(float),
            )
        for sym in REBUILDERS:
            self.rebuilders[sym] = RebuilderModel(drivers=list(REBUILDER_DRIVERS[sym])).fit(
                table, table[f"settle_{sym}"].to_numpy(float)
            )
        return self

    # ---- prediction ----------------------------------------------------
    def predict_stock(self, sym: str, features: dict[str, float]) -> float:
        f = _derive(features)
        if sym in self.islands:
            return self.islands[sym].predict(f.get(f"cumdmg_{sym}", 0.0), f.get(f"outage_{sym}", 0.0))
        if sym in self.insurers:
            return self.insurers[sym].predict(f["season_loss"])
        if sym in self.rebuilders:
            return self.rebuilders[sym].predict(f)
        raise KeyError(sym)

    def predict_all(self, features: dict[str, float]) -> dict[str, float]:
        """Predicted settlement for all 12 symbols given realized-outcome features."""
        out: dict[str, float] = {}
        for sym in ISLAND_BUSINESSES + INSURERS + REBUILDERS:
            out[sym] = self.predict_stock(sym, features)
        for etf, basket in baskets().items():
            out[etf] = sum(out[s] * q for s, q in basket.items())
        return out

    @staticmethod
    def etf_value(stock_values: dict[str, float]) -> dict[str, float]:
        """Basket value of each ETF given a dict of stock values."""
        return {
            etf: sum(stock_values[s] * q for s, q in basket.items())
            for etf, basket in baskets().items()
        }

    # ---- persistence ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "islands": {k: vars(v) for k, v in self.islands.items()},
            "insurers": {k: vars(v) for k, v in self.insurers.items()},
            "rebuilders": {k: vars(v) for k, v in self.rebuilders.items()},
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ValuationModel:
        m = cls()
        m.islands = {k: IslandBusinessModel(**v) for k, v in d["islands"].items()}
        m.insurers = {k: InsurerModel(**v) for k, v in d["insurers"].items()}
        m.rebuilders = {k: RebuilderModel(**v) for k, v in d["rebuilders"].items()}
        return m

    @classmethod
    def load(cls, path: str | Path) -> ValuationModel:
        return cls.from_dict(json.loads(Path(path).read_text()))


def etf_symbols() -> list[str]:
    return list(ETFS)
