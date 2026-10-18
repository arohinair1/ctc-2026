"""Tests for the centralized pre-trade risk manager."""

from __future__ import annotations

from ctc.execution.models import OrderRequest, OrderType, Side
from ctc.execution.risk import RiskManager, gross_exposure, side_of
from ctc.risk.limits import position_limit


def _req(symbol="PORT", side=Side.SELL, qty=10, price=70.0):
    return OrderRequest(symbol, side, qty, OrderType.LIMIT, price=price)


def _eval(rm, req, **kw):
    base = dict(position=0, resting_signed=0, mid=70.0, gross_exposure=0.0, stale=False)
    base.update(kw)
    return rm.evaluate(req, **base)


def test_kill_switch_rejects_all():
    rm = RiskManager(kill_switch=True)
    d = _eval(rm, _req())
    assert not d.approved and d.reason == "kill_switch_engaged"


def test_stale_data_rejected():
    assert _eval(RiskManager(), _req(), stale=True).reason == "stale_market_data"


def test_no_valid_mid_rejected():
    assert _eval(RiskManager(), _req(), mid=None).reason == "no_valid_mid"
    assert _eval(RiskManager(), _req(), mid=0.0).reason == "no_valid_mid"


def test_non_positive_quantity_rejected():
    assert _eval(RiskManager(), _req(qty=0)).reason == "non_positive_quantity"


def test_invalid_limit_price_rejected():
    assert _eval(RiskManager(), _req(price=0.0)).reason == "invalid_limit_price"


def test_position_limit_blocks_when_full():
    lim = position_limit("PORT")
    # already short at the limit; another sell must be rejected
    d = _eval(RiskManager(), _req(side=Side.SELL, qty=10), position=-lim)
    assert not d.approved and d.reason == "at_position_limit"


def test_resting_orders_count_toward_limit():
    lim = position_limit("PORT")
    # flat position but resting short already at the limit -> no room for more shorts
    d = _eval(RiskManager(), _req(side=Side.SELL, qty=10), position=0, resting_signed=-lim)
    assert not d.approved and d.reason == "at_position_limit"


def test_clamp_to_remaining_room():
    lim = position_limit("PORT")
    # room for only 3 more longs
    rm = RiskManager(max_order_size_frac=1.0)
    d = _eval(rm, _req(side=Side.BUY, qty=999), position=lim - 3)
    assert d.approved and d.order is not None
    assert d.order.quantity == 3


def test_max_order_size_cap():
    lim = position_limit("PORT")
    rm = RiskManager(max_order_size_frac=0.25)
    d = _eval(rm, _req(side=Side.BUY, qty=lim))
    assert d.approved
    assert d.order.quantity == max(1, int(0.25 * lim))


def test_gross_exposure_cap():
    rm = RiskManager(max_order_size_frac=1.0, max_gross_exposure=100.0)
    # 10 @ mid 70 = 700 notional > 100 cap
    d = _eval(rm, _req(qty=10), mid=70.0, gross_exposure=0.0)
    assert not d.approved and d.reason == "max_gross_exposure"


def test_duplicate_suppression():
    rm = RiskManager()
    req = _req()
    existing = {req.dedup_key()}
    d = _eval(rm, req, existing_dedup_keys=existing)
    assert not d.approved and d.reason == "duplicate_order"


def test_gross_exposure_helper():
    assert gross_exposure({"PORT": -3, "ISLE": 2}, {"PORT": 10.0, "ISLE": 5.0}) == 40.0
    # missing mid -> symbol skipped
    assert gross_exposure({"PORT": -3}, {}) == 0.0


def test_side_of_helper():
    assert side_of(1) is Side.BUY
    assert side_of(-1) is Side.SELL
