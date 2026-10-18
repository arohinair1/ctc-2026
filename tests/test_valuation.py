"""Tests for the fundamental valuation models."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ctc.config import ALL_SYMBOLS, ETFS, FITTED_PARAMS_JSON, baskets
from ctc.models.valuation import (
    InsurerModel,
    IslandBusinessModel,
    RebuilderModel,
    ValuationModel,
    etf_symbols,
)


def test_island_business_model_recovers_linear_params():
    rng = np.random.default_rng(1)
    cumdmg = rng.uniform(0, 40, 200)
    outage = rng.uniform(0, 500, 200)
    settle = 100.0 - 0.55 * cumdmg - 0.02 * outage
    m = IslandBusinessModel().fit(cumdmg, outage, settle)
    assert abs(m.base - 100.0) < 1e-6
    assert abs(m.b_damage - (-0.55)) < 1e-6
    assert abs(m.c_outage - (-0.02)) < 1e-6
    assert abs(m.predict(10.0, 100.0) - (100.0 - 5.5 - 2.0)) < 1e-6


def test_insurer_model_recovers_layer():
    loss = np.linspace(0, 2000, 400)
    # layer [400, 1300], base 75, rate 0.06 per $M
    settle = 75.0 - 0.06 * np.clip(loss - 400.0, 0.0, 900.0)
    m = InsurerModel().fit(loss, settle)
    # attach / exhaust recovered to grid resolution
    assert abs(m.attach - 400.0) <= 15.0
    assert abs((m.attach + m.width) - 1300.0) <= 20.0
    # below attach -> base; above exhaust -> fully eroded
    assert abs(m.predict(0.0) - 75.0) < 0.5
    assert abs(m.predict(2000.0) - (75.0 - 0.06 * 900.0)) < 1.0
    # payout is the positive erosion
    assert m.payout(1000.0) > 0


def test_rebuilder_model_recovers_sqrt_params():
    rng = np.random.default_rng(2)
    loss = rng.uniform(100, 1700, 300)
    table = pd.DataFrame({"season_loss": loss})
    settle = 25.0 + 0.32 * np.sqrt(loss)
    m = RebuilderModel(drivers=["season_loss"]).fit(table, settle)
    assert abs(m.base - 25.0) < 1e-6
    assert abs(m.coefs[0] - 0.32) < 1e-6
    assert abs(m.predict({"season_loss": 900.0}) - (25.0 + 0.32 * 30.0)) < 1e-6


def test_valuation_model_fit_predict_all(synth_model, synth_table):
    row = synth_table.iloc[0]
    feats = {k: row[k] for k in row.index if k.startswith(("cumdmg_", "outage_"))}
    feats["season_loss"] = row["season_loss"]
    pred = synth_model.predict_all(feats)
    assert set(pred) == set(ALL_SYMBOLS)
    # recovers synthetic settlements closely (exact generative model)
    for sym in ALL_SYMBOLS:
        assert abs(pred[sym] - row[f"settle_{sym}"]) < 1e-3


def test_etf_prediction_equals_basket(synth_model, synth_table):
    row = synth_table.iloc[3]
    feats = {k: row[k] for k in row.index if k.startswith(("cumdmg_", "outage_"))}
    feats["season_loss"] = row["season_loss"]
    pred = synth_model.predict_all(feats)
    for etf, basket in baskets().items():
        assert abs(pred[etf] - sum(pred[s] * q for s, q in basket.items())) < 1e-9


def test_etf_value_static_method():
    stock_values = dict.fromkeys(
        ["PORT", "ISLE", "AQUA", "GRID", "FERRY", "RECON", "MAT", "GEN", "HAVEN", "REIN"], 10.0
    )
    vals = ValuationModel.etf_value(stock_values)
    assert vals["ARCH"] == (1 + 1 + 2 + 1 + 2 + 1) * 10.0
    assert vals["REBLD"] == (2 + 2 + 1) * 10.0


def test_valuation_model_round_trip(synth_model, tmp_path):
    path = tmp_path / "params.json"
    synth_model.save(path)
    loaded = ValuationModel.load(path)
    feats = {f"cumdmg_{s}": 5.0 for s in ["PORT", "ISLE", "AQUA", "GRID", "FERRY"]}
    feats.update({f"outage_{s}": 50.0 for s in ["PORT", "ISLE", "AQUA", "GRID", "FERRY"]})
    feats["season_loss"] = 1000.0
    a = synth_model.predict_all(feats)
    b = loaded.predict_all(feats)
    for sym in ALL_SYMBOLS:
        assert abs(a[sym] - b[sym]) < 1e-12


def test_resid_std_populated(synth_model):
    # every tradable symbol has a residual-std entry (≈0 for the exact synthetic model)
    for sym in ALL_SYMBOLS:
        assert sym in synth_model.resid_std
        assert synth_model.resid_std[sym] >= 0.0


def test_committed_fitted_params_load_and_predict():
    """The packaged fitted_params.json loads and reproduces the insurer layer math."""
    m = ValuationModel.load(FITTED_PARAMS_JSON)
    feats = {f"cumdmg_{s}": 0.0 for s in ["PORT", "ISLE", "AQUA", "GRID", "FERRY"]}
    feats.update({f"outage_{s}": 0.0 for s in ["PORT", "ISLE", "AQUA", "GRID", "FERRY"]})
    feats["season_loss"] = 1000.0
    pred = m.predict_all(feats)
    hv = m.insurers["HAVEN"]
    expected = hv.base + hv.slope * min(max(1000.0 - hv.attach, 0.0), hv.width)
    assert abs(pred["HAVEN"] - expected) < 1e-9
    # all predictions finite
    assert all(np.isfinite(v) for v in pred.values())


def test_etf_symbols_helper():
    assert etf_symbols() == list(ETFS)
