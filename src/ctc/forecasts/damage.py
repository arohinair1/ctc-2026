"""Forecast storm damage and insured losses from observed storm tracks.

Damage is caused by the storm's wind field acting on each company's sites. From the
public storm observations we build physically-motivated wind-exposure features and
regress them onto the per-storm final damage percentages. A separate linear map turns
predicted island-business damage into the storm's insured (industry) loss.

These forecasters are deliberately simple baselines (interpretable, held-out
evaluated). They provide *pre-damage-report* estimates; once a damage report arrives
the exact/observed values supersede the forecast (see :mod:`ctc.forecasts.estimator`).

Uncertainty is reported as held-out residual standard deviations per symbol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ctc.config import ISLAND_BUSINESSES, company_sites
from ctc.models.wind import wind_at


def track_exposure_features(
    obs_list: list[dict[str, Any]], sites: list[dict[str, Any]]
) -> tuple[float, float]:
    """(integrated squared wind, peak wind) for a company given a list of storm obs.

    ``exp2`` ~ cumulative energy (weighted sum of wind^2 * dt); ``peak`` ~ worst
    instantaneous site-weighted wind. Both are monotone in storm severity.
    """
    if not obs_list:
        return 0.0, 0.0
    ages = sorted(o["age"] for o in obs_list)
    diffs = [b - a for a, b in zip(ages, ages[1:], strict=False) if b > a]
    dt = (sorted(diffs)[len(diffs) // 2] if diffs else 2.0) or 2.0
    exp2 = 0.0
    peak = 0.0
    for site in sites:
        sx, sy, w = site["x"], site["y"], site["weight"]
        winds = [wind_at(sx, sy, o) for o in obs_list]
        exp2 += w * sum(ww * ww for ww in winds) * dt
        peak += w * (max(winds) if winds else 0.0)
    return exp2, peak


@dataclass
class DamageForecaster:
    """Per-island-business linear map: (exp2, peak) -> damage_pct, plus a loss map."""

    coefs: dict[str, list[float]] = field(default_factory=dict)  # sym -> [a_exp2, a_peak, intercept]
    resid_std: dict[str, float] = field(default_factory=dict)
    loss_coef: list[float] = field(default_factory=list)  # [w_PORT,...,w_FERRY, intercept]
    loss_resid_std: float = 0.0

    def fit(self, storms: list[dict[str, Any]]) -> DamageForecaster:
        """``storms`` is a list of dicts with keys: features {sym:(exp2,peak)}, damage {sym:pct}, loss."""
        sites = company_sites()  # noqa: F841 (documents dependency)
        for sym in ISLAND_BUSINESSES:
            X = np.array([[s["features"][sym][0], s["features"][sym][1], 1.0] for s in storms])
            y = np.array([s["damage"][sym] for s in storms])
            coef, *_ = np.linalg.lstsq(X, y, rcond=None)
            self.coefs[sym] = [float(c) for c in coef]
            self.resid_std[sym] = float(np.std(X @ coef - y))
        # loss ~ linear in predicted (true) damages
        D = np.array([[s["damage"][sym] for sym in ISLAND_BUSINESSES] + [1.0] for s in storms])
        L = np.array([s["loss"] for s in storms])
        lcoef, *_ = np.linalg.lstsq(D, L, rcond=None)
        self.loss_coef = [float(c) for c in lcoef]
        self.loss_resid_std = float(np.std(D @ lcoef - L))
        return self

    def predict_damage(self, features: dict[str, tuple[float, float]]) -> dict[str, float]:
        out = {}
        for sym in ISLAND_BUSINESSES:
            a = self.coefs[sym]
            exp2, peak = features.get(sym, (0.0, 0.0))
            out[sym] = max(0.0, a[0] * exp2 + a[1] * peak + a[2])
        return out

    def predict_loss(self, damage: dict[str, float]) -> float:
        if not self.loss_coef:
            return 0.0
        v = self.loss_coef[-1]
        for i, sym in enumerate(ISLAND_BUSINESSES):
            v += self.loss_coef[i] * damage.get(sym, 0.0)
        return max(0.0, v)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coefs": self.coefs,
            "resid_std": self.resid_std,
            "loss_coef": self.loss_coef,
            "loss_resid_std": self.loss_resid_std,
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DamageForecaster:
        return cls(
            coefs={k: list(v) for k, v in d["coefs"].items()},
            resid_std=dict(d["resid_std"]),
            loss_coef=list(d["loss_coef"]),
            loss_resid_std=d["loss_resid_std"],
        )

    @classmethod
    def load(cls, path: str | Path) -> DamageForecaster:
        return cls.from_dict(json.loads(Path(path).read_text()))


def build_storm_records(history, season_numbers: list[int]) -> list[dict[str, Any]]:
    """Build per-storm training records (full-track exposure features, damage, loss)."""
    from ctc.models.wind import group_storm_obs

    sites = company_sites()
    records = []
    want = set(season_numbers)
    for season in history.seasons:
        if season.number not in want:
            continue
        obs_by_round = group_storm_obs(season.feed)
        for storm in season.storms:
            r = storm["round"]
            obs = obs_by_round.get(r, [])
            feats = {sym: track_exposure_features(obs, sites[sym]) for sym in ISLAND_BUSINESSES}
            records.append(
                {
                    "features": feats,
                    "damage": {sym: storm["damage_pct"][sym] for sym in ISLAND_BUSINESSES},
                    "loss": storm["industry_loss_musd"],
                }
            )
    return records
