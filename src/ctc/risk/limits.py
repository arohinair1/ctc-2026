"""Position- and order-limit enforcement.

Mirrors the exchange rule (case packet section 8): the position limit applies to both
long and short sides, and includes resting orders on the same side. In the backtest we
hold no resting orders between steps, so the binding constraint is ``|position| <= limit``.
This module is the single place that clamps desired positions/orders to the limits so the
same logic can be reused by the live execution layer.
"""

from __future__ import annotations

from ctc.config import limits as _case_limits


def position_limit(symbol: str) -> int:
    return int(_case_limits()[symbol])


def clamp_target(symbol: str, target: float) -> int:
    """Clamp a desired target position to the symbol's position limit."""
    lim = position_limit(symbol)
    return int(max(-lim, min(lim, round(target))))


def clamp_order(symbol: str, current: int, desired_target: int, max_trade: int | None = None) -> int:
    """Return an order quantity (signed) that respects the limit and optional per-step cap.

    Positive = buy, negative = sell. The resulting position never exceeds the limit.
    """
    lim = position_limit(symbol)
    target = max(-lim, min(lim, desired_target))
    order = target - current
    if max_trade is not None:
        order = max(-max_trade, min(max_trade, order))
    # final safety: ensure resulting position within limit
    new_pos = current + order
    if new_pos > lim:
        order = lim - current
    elif new_pos < -lim:
        order = -lim - current
    return int(order)
