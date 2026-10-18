"""Tests for the track-based damage/loss forecaster."""

from __future__ import annotations

import numpy as np

from ctc.config import ISLAND_BUSINESSES
from ctc.forecasts.damage import DamageForecaster, track_exposure_features
from tests.conftest import make_storm_obs


def test_track_exposure_features_empty():
    assert track_exposure_features([], [{"x": 0, "y": 0, "weight": 1.0}]) == (0.0, 0.0)


def test_track_exposure_features_positive_and_monotone():
    sites = [{"x": 0.0, "y": 0.0, "weight": 1.0}]
    weak = [make_storm_obs(1, age=a, x=0.0, y=0.0, intensity=0.5) for a in (0, 2, 4)]
    strong = [make_storm_obs(1, age=a, x=0.0, y=0.0, intensity=1.0) for a in (0, 2, 4)]
    e_weak, p_weak = track_exposure_features(weak, sites)
    e_strong, p_strong = track_exposure_features(strong, sites)
    assert e_strong > e_weak > 0
    assert p_strong > p_weak > 0


def _synthetic_storm_records(n=120, seed=0):
    """Records whose damage is an exact linear function of (exp2, peak)."""
    rng = np.random.default_rng(seed)
    coefs = {sym: (0.5 + i, 2.0 + i, 1.0) for i, sym in enumerate(ISLAND_BUSINESSES)}
    recs = []
    for _ in range(n):
        feats = {}
        damage = {}
        for sym, (a0, a1, a2) in coefs.items():
            exp2 = float(rng.uniform(0, 100))
            peak = float(rng.uniform(0, 5))
            feats[sym] = (exp2, peak)
            damage[sym] = a0 * exp2 + a1 * peak + a2
        loss = sum(damage.values()) * 0.01
        recs.append({"features": feats, "damage": damage, "loss": loss})
    return recs, coefs


def test_forecaster_recovers_linear_map():
    recs, coefs = _synthetic_storm_records()
    fc = DamageForecaster().fit(recs)
    for sym, (a0, a1, a2) in coefs.items():
        got = fc.coefs[sym]
        assert abs(got[0] - a0) < 1e-6
        assert abs(got[1] - a1) < 1e-6
        assert abs(got[2] - a2) < 1e-6
        # residuals ~ 0 for the exact synthetic model
        assert fc.resid_std[sym] < 1e-6


def test_predict_damage_clamps_at_zero():
    recs, _ = _synthetic_storm_records()
    fc = DamageForecaster().fit(recs)
    # zero exposure -> intercept is positive here, but a strongly negative intercept
    # scenario is covered by clamping: craft coefs directly
    fc.coefs["PORT"] = [1.0, 1.0, -100.0]
    out = fc.predict_damage({"PORT": (0.0, 0.0)})
    assert out["PORT"] == 0.0


def test_predict_loss_linear_and_nonnegative():
    recs, _ = _synthetic_storm_records()
    fc = DamageForecaster().fit(recs)
    dmg = dict.fromkeys(ISLAND_BUSINESSES, 10.0)
    loss = fc.predict_loss(dmg)
    assert loss >= 0.0
    # empty loss_coef -> 0
    fc2 = DamageForecaster()
    assert fc2.predict_loss(dmg) == 0.0


def test_forecaster_round_trip(tmp_path):
    recs, _ = _synthetic_storm_records()
    fc = DamageForecaster().fit(recs)
    path = tmp_path / "fc.json"
    fc.save(path)
    loaded = DamageForecaster.load(path)
    feats = {sym: (10.0, 1.0) for sym in ISLAND_BUSINESSES}
    a = fc.predict_damage(feats)
    b = loaded.predict_damage(feats)
    for sym in ISLAND_BUSINESSES:
        assert abs(a[sym] - b[sym]) < 1e-12
    assert abs(fc.loss_resid_std - loaded.loss_resid_std) < 1e-12
