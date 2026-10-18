"""End-to-end integration tests against the real research dataset.

These are skipped automatically when the dataset is not available (e.g. in CI), so the
core unit suite stays data-independent. They guard the glue between the loader, feature
builder, estimator and backtester on a couple of real seasons.
"""

from __future__ import annotations

import math

from ctc.config import ALL_SYMBOLS, ISLAND_BUSINESSES
from ctc.data.features import build_feature_table, season_cumulative_damage, season_outages
from ctc.data.history import load
from ctc.data.validate import validate_season
from ctc.forecasts.damage import DamageForecaster, build_storm_records
from ctc.forecasts.estimator import SeasonEstimator, compute_priors
from ctc.models.valuation import ValuationModel
from tests.conftest import requires_data

pytestmark = requires_data


def _small_history(n=6):
    data = load()
    data.seasons = data.seasons[:n]
    return data


def test_load_and_validate_real_seasons():
    data = _small_history(4)
    assert len(data.seasons) == 4
    for s in data.seasons:
        issues = validate_season(s)
        assert issues.errors == [], f"season {s.number}: {issues.errors}"
        # each season has all 12 settlement symbols
        assert set(ALL_SYMBOLS) <= set(s.settlement)


def test_feature_table_shapes():
    data = _small_history(5)
    table = build_feature_table(data)
    assert len(table) == 5
    for sym in ISLAND_BUSINESSES:
        assert f"cumdmg_{sym}" in table.columns
        assert f"outage_{sym}" in table.columns
    for sym in ALL_SYMBOLS:
        assert f"settle_{sym}" in table.columns


def test_valuation_fits_and_predicts_on_real_data():
    data = _small_history(12)
    table = build_feature_table(data)
    model = ValuationModel().fit(table)
    # fit then predict on the first season; island/insurer fits are near-exact historically
    row = table.iloc[0]
    feats = {k: row[k] for k in row.index if k.startswith(("cumdmg_", "outage_"))}
    feats["season_loss"] = row["season_loss"]
    pred = model.predict_all(feats)
    # ISLE/AQUA are ~pure-damage linear (R^2≈1) -> small error
    assert abs(pred["ISLE"] - row["settle_ISLE"]) < 2.0
    assert abs(pred["AQUA"] - row["settle_AQUA"]) < 2.0


def test_estimator_matches_feature_table_after_full_feed():
    """After ingesting a whole season's feed, the leakage-safe estimator's cumulative
    damage/outage must match the offline feature-table ground truth for that season."""
    data = _small_history(10)
    table = build_feature_table(data)
    model = ValuationModel().fit(table)
    nums = [s.number for s in data.seasons]
    forecaster = DamageForecaster().fit(build_storm_records(data, nums))
    priors = compute_priors(data, nums)
    est = SeasonEstimator(model, forecaster, priors)

    season = data.seasons[0]
    est.reset()
    for msg in sorted(season.feed, key=lambda m: (m.get("time", ""), m.get("seq", 0))):
        est.ingest(msg)
    feats = est.estimate_final_features()

    cum = season_cumulative_damage(season)
    out = season_outages(season)
    for sym in ISLAND_BUSINESSES:
        assert math.isclose(feats[f"cumdmg_{sym}"], cum[sym], abs_tol=0.1)
        assert math.isclose(feats[f"outage_{sym}"], out[sym], rel_tol=1e-6, abs_tol=1e-6)
    assert math.isclose(feats["season_loss"], season.season_loss_musd, abs_tol=1.0)


def test_backtest_single_real_season_runs():
    from ctc.backtest.engine import Backtester
    from ctc.strategy.signals import StrategyConfig

    data = _small_history(10)
    table = build_feature_table(data)
    model = ValuationModel().fit(table)
    nums = [s.number for s in data.seasons]
    forecaster = DamageForecaster().fit(build_storm_records(data, nums))
    priors = compute_priors(data, nums)
    bt = Backtester(SeasonEstimator(model, forecaster, priors), StrategyConfig())
    res = bt.run_season(data.seasons[0])
    assert abs(sum(res.pnl_by_symbol.values()) - res.pnl) < 1e-3
    assert res.n_trades >= 0
