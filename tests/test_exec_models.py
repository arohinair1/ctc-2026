"""Tests for exchange protocol data models and parsers."""

from __future__ import annotations

import pytest

from ctc.execution.models import (
    OrderAck,
    OrderBook,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    Side,
)


def test_quote_parse_and_mid():
    q = Quote.parse({"symbol": "PORT", "bid": 72.0, "ask": 72.4, "time": "t"}, received_at=1.0)
    assert q.symbol == "PORT"
    assert q.mid == pytest.approx(72.2)
    assert q.received_at == 1.0
    # missing side -> mid None
    assert Quote.parse({"symbol": "X", "bid": 1.0, "ask": None}).mid is None


def test_orderbook_parse_sorts_levels():
    ob = OrderBook.parse(
        {
            "bids": [{"price": 10.0, "size": 5}, {"price": 11.0, "size": 2}],
            "asks": [[13.0, 1], [12.0, 4]],
        },
        symbol="GRID",
    )
    assert ob.best_bid == 11.0  # highest bid first
    assert ob.best_ask == 12.0  # lowest ask first
    assert ob.mid == pytest.approx(11.5)


def test_order_request_payload_limit_and_market():
    lim = OrderRequest("PORT", Side.BUY, 10, OrderType.LIMIT, price=72.123456789)
    payload = lim.to_payload()
    assert payload == {
        "symbol": "PORT",
        "side": "buy",
        "order_type": "limit",
        "quantity": 10,
        "price": round(72.123456789, 6),
    }
    mkt = OrderRequest("PORT", Side.SELL, 5, OrderType.MARKET)
    assert "price" not in mkt.to_payload()


def test_limit_order_requires_price():
    with pytest.raises(ValueError):
        OrderRequest("PORT", Side.BUY, 1, OrderType.LIMIT, price=None).to_payload()


def test_signed_qty_and_side_of_order():
    assert OrderRequest("X", Side.BUY, 3).signed_qty == 3
    assert OrderRequest("X", Side.SELL, 3).signed_qty == -3
    assert Side.of_order(5) is Side.BUY
    assert Side.of_order(-5) is Side.SELL


def test_dedup_key_distinguishes_side_and_price():
    a = OrderRequest("PORT", Side.BUY, 10, OrderType.LIMIT, price=72.0)
    b = OrderRequest("PORT", Side.SELL, 10, OrderType.LIMIT, price=72.0)
    c = OrderRequest("PORT", Side.BUY, 99, OrderType.LIMIT, price=72.0)
    assert a.dedup_key() != b.dedup_key()
    assert a.dedup_key() == c.dedup_key()  # quantity not part of identity


def test_order_ack_parse_variants():
    ack = OrderAck.parse({"order_id": "o1", "symbol": "PORT", "status": "accepted"})
    assert ack.order_id == "o1" and ack.status == "accepted"
    ack2 = OrderAck.parse({"id": "o2", "symbol": "ISLE", "filled_quantity": 3})
    assert ack2.order_id == "o2" and ack2.filled == 3.0


def test_position_parse():
    p = Position.parse({"symbol": "HAVEN", "quantity": -12, "mark": 39.0, "unrealized_pnl": 5.0})
    assert p.quantity == -12 and p.mark == 39.0
    assert Position.parse({"symbol": "X", "position": 7}).quantity == 7
