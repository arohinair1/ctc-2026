"""Tests for position/order limit enforcement."""

from __future__ import annotations

from ctc.config import limits
from ctc.risk.limits import clamp_order, clamp_target, position_limit


def test_position_limit_matches_case():
    for sym, lim in limits().items():
        assert position_limit(sym) == lim


def test_clamp_target_bounds():
    lim = position_limit("PORT")  # 200
    assert clamp_target("PORT", 10_000) == lim
    assert clamp_target("PORT", -10_000) == -lim
    assert clamp_target("PORT", 50.6) == 51  # rounds


def test_clamp_order_respects_limit():
    lim = position_limit("PORT")  # 200
    # want to go to +500 but limit is 200, currently flat
    assert clamp_order("PORT", current=0, desired_target=500) == lim
    # currently at limit, want more -> no order
    assert clamp_order("PORT", current=lim, desired_target=500) == 0
    # short side symmetric
    assert clamp_order("PORT", current=0, desired_target=-500) == -lim


def test_clamp_order_max_trade_cap():
    # want +200 but capped at 10 per step
    assert clamp_order("PORT", current=0, desired_target=200, max_trade=10) == 10
    assert clamp_order("PORT", current=0, desired_target=-200, max_trade=10) == -10


def test_clamp_order_never_breaches_limit_with_cap():
    lim = position_limit("GRID")
    order = clamp_order("GRID", current=lim - 3, desired_target=lim + 100, max_trade=50)
    assert order == 3  # only room for 3 more
    assert (lim - 3) + order <= lim


def test_clamp_order_reduces_position():
    # long 100, want flat
    assert clamp_order("AQUA", current=100, desired_target=0) == -100
