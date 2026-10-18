"""Tests for portfolio accounting and the event-driven backtester."""

from __future__ import annotations

from ctc.backtest.accounting import Portfolio
from ctc.backtest.engine import Backtester, SeasonResult
from ctc.config import ALL_SYMBOLS, ISLAND_BUSINESSES
from ctc.strategy.signals import StrategyConfig
from tests.conftest import FakeSeason, make_damage_report
from tests.test_estimator import _flat_priors, _simple_forecaster

# ---- Portfolio accounting --------------------------------------------------


def test_fill_updates_cash_and_position():
    pf = Portfolio()
    pf.fill("PORT", qty=10, price=101.0, mid=100.0)
    assert pf.positions["PORT"] == 10
    assert pf.cash == -1010.0
    assert pf.n_trades == 1
    assert pf.turnover == 10 * 100.0
    assert pf.spread_cost == 10 * 1.0  # |101-100| per share


def test_fill_zero_qty_is_noop():
    pf = Portfolio()
    pf.fill("PORT", qty=0, price=100.0, mid=100.0)
    assert pf.n_trades == 0
    assert pf.positions == {}


def test_sell_increases_cash():
    pf = Portfolio()
    pf.fill("PORT", qty=-5, price=99.0, mid=100.0)
    assert pf.positions["PORT"] == -5
    assert pf.cash == 495.0
    assert pf.spread_cost == 5 * 1.0


def test_equity_and_settle():
    pf = Portfolio()
    pf.fill("PORT", qty=10, price=100.0, mid=100.0)  # cash -1000, +10 PORT
    assert pf.equity({"PORT": 100.0}) == 0.0
    assert pf.equity({"PORT": 120.0}) == 200.0
    # settlement realizes the mark
    assert pf.settle({"PORT": 120.0}) == 200.0


def test_update_drawdown_tracks_trough():
    pf = Portfolio()
    pf.fill("PORT", qty=10, price=100.0, mid=100.0)
    pf.update_drawdown({"PORT": 100.0})  # equity 0, peak 0
    pf.update_drawdown({"PORT": 90.0})  # equity -100 -> drawdown -100
    pf.update_drawdown({"PORT": 130.0})  # equity +300 -> new peak
    assert pf.max_drawdown == -100.0
    assert pf.peak_equity == 300.0


# ---- Backtester integration ------------------------------------------------


def _make_season() -> FakeSeason:
    times = ["2025-08-04T10:00:00", "2025-08-04T12:00:00", "2025-08-04T15:40:00"]
    # cheap mids so the model's (higher) fair values trigger buys
    prices = {s: [1.0, 1.0, 1.0] for s in ALL_SYMBOLS}
    # a final damage report arrives by the last sample
    dmg = dict.fromkeys(ISLAND_BUSINESSES, 5.0)
    feed = [make_damage_report(1, dmg, 120.0, stage="final", time="2025-08-04T12:00:00")]
    # settlement values for all symbols (positive)
    settlement = {s: 50.0 for s in ALL_SYMBOLS}
    return FakeSeason(number=7, storms=[], feed=feed, settlement=settlement, prices=prices, times=times)


def test_backtester_runs_and_pnl_identity(synth_model):
    from ctc.forecasts.estimator import SeasonEstimator

    est = SeasonEstimator(synth_model, _simple_forecaster(), _flat_priors())
    bt = Backtester(est, StrategyConfig())
    res = bt.run_season(_make_season())

    assert isinstance(res, SeasonResult)
    assert res.season == 7
    # PnL attribution identity: per-symbol PnL sums to total PnL
    assert abs(sum(res.pnl_by_symbol.values()) - res.pnl) < 1e-6
    # bought cheap (mid=1) and settled at 50 -> profitable, with trades executed
    assert res.n_trades > 0
    assert res.pnl > 0
    assert res.turnover > 0
    assert res.max_drawdown <= 0.0


def test_backtester_respects_position_limits(synth_model):
    from ctc.config import limits
    from ctc.forecasts.estimator import SeasonEstimator

    est = SeasonEstimator(synth_model, _simple_forecaster(), _flat_priors())
    bt = Backtester(est, StrategyConfig(max_trade_per_step_frac=1.0))
    res = bt.run_season(_make_season())
    lim = limits()
    for sym, pos in res.final_positions.items():
        assert abs(pos) <= lim[sym]
