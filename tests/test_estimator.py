"""Tests for the leakage-safe running season estimator.

The central property is *no look-ahead*: at any point the estimator may only use
information already in the feed. Once a round's FINAL damage report has arrived, the
estimate for that round must equal the reported values exactly (no prior/forecaster
blending), and uncertainty must collapse to the model's irreducible residual.
"""

from __future__ import annotations

import math

from ctc.config import ISLAND_BUSINESSES
from ctc.forecasts.damage import DamageForecaster
from ctc.forecasts.estimator import Priors, SeasonEstimator
from tests.conftest import make_damage_report, make_storm_obs


def _flat_priors() -> Priors:
    rounds = (1, 2, 3)
    dmg = {r: dict.fromkeys(ISLAND_BUSINESSES, 5.0) for r in rounds}
    outage = {r: dict.fromkeys(ISLAND_BUSINESSES, 10.0) for r in rounds}
    dvar = {r: dict.fromkeys(ISLAND_BUSINESSES, 4.0) for r in rounds}
    ovar = {r: dict.fromkeys(ISLAND_BUSINESSES, 9.0) for r in rounds}
    return Priors(
        damage_by_round=dmg,
        loss_by_round=dict.fromkeys(rounds, 100.0),
        outage_by_round=outage,
        damage_var_by_round=dvar,
        loss_var_by_round=dict.fromkeys(rounds, 25.0),
        outage_var_by_round=ovar,
        prelim_damage_err_var=dict.fromkeys(ISLAND_BUSINESSES, 1.0),
        prelim_loss_err_var=16.0,
    )


def _simple_forecaster() -> DamageForecaster:
    fc = DamageForecaster()
    fc.coefs = {sym: [0.0, 0.0, 3.0] for sym in ISLAND_BUSINESSES}  # predicts 3.0 each
    fc.resid_std = dict.fromkeys(ISLAND_BUSINESSES, 2.0)
    fc.loss_coef = [0.0] * len(ISLAND_BUSINESSES) + [50.0]  # constant 50
    fc.loss_resid_std = 5.0
    return fc


def _make_estimator(synth_model):
    return SeasonEstimator(synth_model, _simple_forecaster(), _flat_priors())


def test_priors_used_before_any_information(synth_model):
    est = _make_estimator(synth_model)
    feats = est.estimate_final_features()
    # 3 rounds * prior 5.0 damage each, 3 * 100 loss
    for sym in ISLAND_BUSINESSES:
        assert math.isclose(feats[f"cumdmg_{sym}"], 15.0)
    assert math.isclose(feats["season_loss"], 300.0)


def test_final_reports_are_exact_and_leakage_safe(synth_model):
    est = _make_estimator(synth_model)
    reports = {
        1: ({"PORT": 10.0, "ISLE": 2.0, "AQUA": 3.0, "GRID": 4.0, "FERRY": 5.0}, 150.0),
        2: ({"PORT": 1.0, "ISLE": 1.0, "AQUA": 1.0, "GRID": 1.0, "FERRY": 1.0}, 90.0),
        3: ({"PORT": 0.0, "ISLE": 8.0, "AQUA": 6.0, "GRID": 2.0, "FERRY": 3.0}, 200.0),
    }
    for r, (dmg, loss) in reports.items():
        est.ingest(make_damage_report(r, dmg, loss, stage="final"))

    feats, var = est._estimate()
    for sym in ISLAND_BUSINESSES:
        expected = sum(reports[r][0][sym] for r in (1, 2, 3))
        assert math.isclose(feats[f"cumdmg_{sym}"], expected)
        assert var[f"cumdmg_{sym}"] == 0.0  # no uncertainty once all finals are in
    assert math.isclose(feats["season_loss"], 150.0 + 90.0 + 200.0)
    assert var["season_loss"] == 0.0


def test_fair_values_match_valuation_on_reported_features(synth_model):
    est = _make_estimator(synth_model)
    dmg = {"PORT": 10.0, "ISLE": 2.0, "AQUA": 3.0, "GRID": 4.0, "FERRY": 5.0}
    for r in (1, 2, 3):
        est.ingest(make_damage_report(r, dmg, 120.0, stage="final"))
    feats = est.estimate_final_features()
    fair = est.fair_values()
    assert fair == synth_model.predict_all(feats)


def test_uncertainty_collapses_to_residual_floor_after_finals(synth_model):
    est = _make_estimator(synth_model)
    # before any info: uncertainty is large (priors have variance)
    _, std_before = est.fair_values_with_uncertainty()
    dmg = dict.fromkeys(ISLAND_BUSINESSES, 5.0)
    for r in (1, 2, 3):
        est.ingest(make_damage_report(r, dmg, 120.0, stage="final"))
    _, std_after = est.fair_values_with_uncertainty()
    for sym in ISLAND_BUSINESSES:
        # after all finals, feature variance is 0 so std equals the residual floor
        assert math.isclose(std_after[sym], synth_model.resid_std[sym], abs_tol=1e-9)
        assert std_after[sym] <= std_before[sym] + 1e-9


def test_preliminary_report_used_until_final(synth_model):
    est = _make_estimator(synth_model)
    prelim = dict.fromkeys(ISLAND_BUSINESSES, 7.0)
    est.ingest(make_damage_report(1, prelim, 110.0, stage="preliminary"))
    feats = est.estimate_final_features()
    # round 1 uses prelim (7.0), rounds 2,3 use priors (5.0 each) -> 7 + 5 + 5 = 17
    for sym in ISLAND_BUSINESSES:
        assert math.isclose(feats[f"cumdmg_{sym}"], 17.0)
    # now the final supersedes the preliminary
    final = dict.fromkeys(ISLAND_BUSINESSES, 9.0)
    est.ingest(make_damage_report(1, final, 130.0, stage="final"))
    feats2 = est.estimate_final_features()
    for sym in ISLAND_BUSINESSES:
        assert math.isclose(feats2[f"cumdmg_{sym}"], 9.0 + 5.0 + 5.0)


def test_forecaster_used_once_storm_observed(synth_model):
    est = _make_estimator(synth_model)
    # observe a storm in round 1 (no report yet) -> forecaster predicts 3.0 for round 1
    est.ingest(make_storm_obs(1, age=0, x=470.0, y=80.0))
    feats = est.estimate_final_features()
    # round 1 forecaster (3.0) + rounds 2,3 priors (5.0) = 3 + 5 + 5 = 13
    for sym in ISLAND_BUSINESSES:
        assert math.isclose(feats[f"cumdmg_{sym}"], 13.0)


def test_reset_clears_state(synth_model):
    est = _make_estimator(synth_model)
    est.ingest(make_damage_report(1, dict.fromkeys(ISLAND_BUSINESSES, 9.0), 130.0, stage="final"))
    est.reset()
    assert est.reported_rounds == set()
    assert est.reported_loss == 0.0
    # back to pure priors
    assert math.isclose(est.estimate_final_features()["season_loss"], 300.0)
