"""Centralized pre-trade risk checks and execution controls.

Every proposed order passes through :meth:`RiskManager.evaluate` before it can be
submitted. Checks (in order): kill switch, stale/invalid market data, position-limit
clamp (including resting orders, reusing :mod:`ctc.risk.limits`), max per-order size,
duplicate-order suppression, and max gross exposure. The manager is pure and synchronous
so it is trivially unit-testable; the dry-run gate (never submitting) is enforced by the
executor in :mod:`ctc.execution.trader`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ctc.execution.log import get_logger
from ctc.execution.models import OrderRequest, Side
from ctc.risk.limits import position_limit

log = get_logger("risk")


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    order: OrderRequest | None = None  # possibly size-adjusted


@dataclass
class RiskManager:
    max_order_size_frac: float = 0.25
    max_gross_exposure: float = 2_000_000.0
    kill_switch: bool = False
    max_order_size_units: int | None = None

    def max_order_size(self, symbol: str) -> int:
        fractional = max(1, int(self.max_order_size_frac * position_limit(symbol)))
        if self.max_order_size_units is not None:
            return min(fractional, max(1, self.max_order_size_units))
        return fractional

    def evaluate(
        self,
        req: OrderRequest,
        *,
        position: int,
        resting_signed: int,
        mid: float | None,
        gross_exposure: float,
        stale: bool,
        existing_dedup_keys: set[tuple] | None = None,
    ) -> RiskDecision:
        existing_dedup_keys = existing_dedup_keys or set()

        if self.kill_switch:
            return self._reject(req, "kill_switch_engaged")
        if stale:
            return self._reject(req, "stale_market_data")
        if mid is None or mid <= 0:
            return self._reject(req, "no_valid_mid")
        if req.quantity <= 0:
            return self._reject(req, "non_positive_quantity")
        if req.order_type.value == "limit" and (req.price is None or req.price <= 0):
            return self._reject(req, "invalid_limit_price")

        lim = position_limit(req.symbol)
        signed = req.signed_qty
        # room under the limit given current position AND resting orders on the same side
        effective = position + resting_signed
        if signed > 0:
            room = lim - effective
        else:
            room = -lim - effective  # negative
        if (signed > 0 and room <= 0) or (signed < 0 and room >= 0):
            return self._reject(req, "at_position_limit")

        # clamp to available room
        clamped = signed
        if signed > 0:
            clamped = min(signed, room)
        else:
            clamped = max(signed, room)

        # clamp to max per-order size
        mos = self.max_order_size(req.symbol)
        if abs(clamped) > mos:
            clamped = mos if clamped > 0 else -mos

        if clamped == 0:
            return self._reject(req, "clamped_to_zero")

        # gross-exposure cap (projected)
        exposure_change = (abs(position + clamped) - abs(position)) * mid
        projected = gross_exposure + exposure_change
        # Inherited manual holdings may already exceed the cap. Allow orders
        # that strictly reduce that exposure; continue blocking every increase.
        if projected > self.max_gross_exposure and exposure_change >= 0:
            return self._reject(req, "max_gross_exposure")

        adjusted = OrderRequest(
            symbol=req.symbol,
            side=req.side,
            quantity=abs(clamped),
            order_type=req.order_type,
            price=req.price,
            client_tag=req.client_tag,
        )

        # duplicate suppression (same symbol/side/type/price already resting)
        if adjusted.dedup_key() in existing_dedup_keys:
            return self._reject(adjusted, "duplicate_order")

        log.info(
            "APPROVE %s %s x%d @ %s (pos=%d resting=%d)",
            adjusted.symbol,
            adjusted.side.value,
            adjusted.quantity,
            adjusted.price,
            position,
            resting_signed,
        )
        return RiskDecision(True, "ok", adjusted)

    def _reject(self, req: OrderRequest, reason: str) -> RiskDecision:
        log.info("REJECT %s %s x%d: %s", req.symbol, req.side.value, req.quantity, reason)
        return RiskDecision(False, reason, None)


def gross_exposure(positions: dict[str, int], mids: dict[str, float]) -> float:
    """Sum of |position| * mid across symbols with a known mid."""
    total = 0.0
    for sym, pos in positions.items():
        if pos and sym in mids:
            total += abs(pos) * mids[sym]
    return total


def side_of(signed_qty: int) -> Side:
    return Side.BUY if signed_qty > 0 else Side.SELL
