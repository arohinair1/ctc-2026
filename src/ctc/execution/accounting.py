"""Authoritative cumulative PnL and order status from the verified exchange API.

The web application's API client uses include_zero=true for positions and GET /orders
for account order history. Closed positions must be included to retain realized PnL.
Account PnL replaces the need to reconstruct cash from fill prices; order IDs, quantities
and terminal states are still reconciled separately. No equity value or fill is invented.
"""

from __future__ import annotations

import math
import time
from typing import Any

from ctc.config import ALL_SYMBOLS
from ctc.execution.automation import VerifiedOrderState, VerifiedPnLSnapshot
from ctc.execution.client import ExchangeClient


def _number(row: dict[str, Any], name: str) -> float:
    value = row[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"account field {name} is not a finite number")
    return float(value)


def parse_account_snapshot(positions: Any, orders: Any, *, as_of: float) -> VerifiedPnLSnapshot:
    if not isinstance(positions, dict) or not isinstance(positions.get("positions"), list):
        raise ValueError("positions response is missing its complete positions list")
    if not isinstance(orders, dict) or not isinstance(orders.get("orders"), list):
        raise ValueError("orders response is missing its order list")
    realized = unrealized = 0.0
    symbols: set[str] = set()
    for row in positions["positions"]:
        if not isinstance(row, dict) or row.get("symbol") not in ALL_SYMBOLS:
            raise ValueError("unexpected position symbol or row")
        symbol = row["symbol"]
        if symbol in symbols:
            raise ValueError("duplicate position symbol")
        symbols.add(symbol)
        quantity = _number(row, "quantity")
        if quantity != int(quantity):
            raise ValueError("position quantity is not an integer")
        realized += _number(row, "realized_pnl")
        # The verified API returns null unrealized PnL for a flat position with
        # no average entry price. Its unrealized exposure is exactly zero.
        if quantity == 0 and row.get("unrealized_pnl") is None:
            pass
        else:
            unrealized += _number(row, "unrealized_pnl")
    states = []
    ids: set[str] = set()
    for row in orders["orders"]:
        if not isinstance(row, dict):
            raise ValueError("invalid order row")
        oid = row.get("order_id")
        if not isinstance(oid, str) or not oid or oid in ids:
            raise ValueError("missing or duplicate account order ID")
        ids.add(oid)
        quantity = _number(row, "quantity")
        filled = _number(row, "filled_quantity")
        if quantity != int(quantity) or quantity <= 0 or not 0 <= filled <= quantity:
            raise ValueError("invalid account order quantities")
        if row.get("symbol") not in ALL_SYMBOLS or row.get("side") not in {"buy", "sell"}:
            raise ValueError("unexpected account order symbol or side")
        status = row.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError("missing account order status")
        states.append(VerifiedOrderState(oid, row["symbol"], row["side"], int(quantity), filled, status))
    return VerifiedPnLSnapshot(
        starting_equity=None,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        as_of=as_of,
        source_verified=True,
        execution_history_complete=False,
        account_pnl_complete=True,
        order_states=tuple(states),
    )


async def read_snapshot(client: ExchangeClient) -> VerifiedPnLSnapshot:
    # Timestamp the beginning, so a slow request cannot make old account data look fresh.
    as_of = time.time()
    before = await client.get_time()
    positions = await client._request("GET", "/positions?include_zero=true")
    orders = await client._request("GET", "/orders?limit=500")
    after = await client.get_time()
    if not before.get("competition_id") or before.get("competition_id") != after.get("competition_id"):
        raise ValueError("competition changed during account snapshot")
    return parse_account_snapshot(positions, orders, as_of=as_of)
