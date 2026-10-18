"""Tests for the baseline trading strategy / decision logic."""

from __future__ import annotations

from ctc.risk.limits import position_limit
from ctc.strategy.signals import Decision, StrategyConfig, decide, executable_prices


def test_executable_prices():
    cfg = StrategyConfig(half_spread=0.01)
    bid, ask = executable_prices(100.0, cfg)
    assert bid == 99.0
    assert ask == 101.0


def test_buy_when_fair_far_above_mid():
    cfg = StrategyConfig()
    fair = {"PORT": 120.0}
    mids = {"PORT": 100.0}
    decisions = {d.symbol: d for d in decide(fair, mids, {}, cfg)}
    d = decisions["PORT"]
    assert d.order > 0  # buying
    assert d.executable > mids["PORT"]  # transacts at the ask
    assert d.edge == fair["PORT"] - mids["PORT"]


def test_sell_when_fair_far_below_mid():
    cfg = StrategyConfig()
    fair = {"PORT": 80.0}
    mids = {"PORT": 100.0}
    d = decide(fair, mids, {}, cfg)[0]
    assert d.order < 0
    assert d.executable < mids["PORT"]  # transacts at the bid


def test_no_trade_within_cushion():
    cfg = StrategyConfig()
    # fair essentially equals mid -> edge below required cushion+spread
    fair = {"PORT": 100.01}
    mids = {"PORT": 100.0}
    d = decide(fair, mids, {}, cfg)[0]
    assert d.order == 0
    assert d.target == 0


def test_uncertainty_gates_trading():
    cfg = StrategyConfig(uncertainty_k=1.5)
    fair = {"PORT": 120.0}
    mids = {"PORT": 100.0}
    # a 20-point edge is normally a strong buy...
    assert decide(fair, mids, {}, cfg)[0].order > 0
    # ...but a huge uncertainty (sigma=100) makes required edge exceed it -> no trade
    d = decide(fair, mids, {}, cfg, uncertainty={"PORT": 100.0})[0]
    assert d.order == 0


def test_position_scales_with_edge_and_respects_limit():
    cfg = StrategyConfig(max_trade_per_step_frac=1.0)  # allow full move in one step
    lim = position_limit("PORT")
    # a massive edge should target the full position limit
    d = decide({"PORT": 1000.0}, {"PORT": 100.0}, {}, cfg)[0]
    assert d.target == lim
    assert 0 < d.order <= lim


def test_max_trade_per_step_cap():
    cfg = StrategyConfig(max_trade_per_step_frac=0.1)
    lim = position_limit("PORT")
    d = decide({"PORT": 1000.0}, {"PORT": 100.0}, {}, cfg)[0]
    # order limited to 10% of the limit per step even though target is the full limit
    assert d.order <= max(1, int(0.1 * lim))
    assert d.target == lim


def test_skips_symbols_without_price_or_fair():
    cfg = StrategyConfig()
    decisions = decide({"PORT": 120.0}, {"PORT": None, "ISLE": 50.0}, {}, cfg)
    syms = {d.symbol for d in decisions}
    assert "PORT" not in syms  # mid is None
    assert "ISLE" not in syms  # fair missing


def test_decision_dataclass_shape():
    d = Decision(symbol="X", target=1, order=1, edge=0.5, executable=10.0)
    assert d.symbol == "X" and d.order == 1


def test_tradable_filter_gates_symbols():
    cfg = StrategyConfig()
    fair = {"PORT": 120.0, "ISLE": 70.0}
    mids = {"PORT": 100.0, "ISLE": 50.0}
    # with PORT excluded from the tradable set, no PORT decision is produced
    syms = {d.symbol for d in decide(fair, mids, {}, cfg, tradable={"ISLE"})}
    assert syms == {"ISLE"}
    # None (default) => both symbols considered
    syms_all = {d.symbol for d in decide(fair, mids, {}, cfg, tradable=None)}
    assert {"PORT", "ISLE"} <= syms_all


def test_gate_until_report_is_opt_in_default_empty():
    # the baseline config does not gate anything
    assert StrategyConfig().gate_until_report == frozenset()
