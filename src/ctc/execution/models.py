"""Type-safe data models for the exchange protocol.

Shapes follow the case packet appendix and the starter-kit ``example_bot.py`` /
``record_feed.py`` exactly:

* Market-data WebSocket frames are ``{"type": ..., ...}``. ``news`` frames wrap a feed
  message in ``{"type": "news", "message": {...}}``; ``quote`` frames carry ``symbol`` /
  ``bid`` / ``ask``. The feed message types (storm, track, advisory, impact,
  damage_report, snapshot, ...) match the research ``feed.jsonl`` schema, so they are fed
  straight into :class:`ctc.forecasts.estimator.SeasonEstimator`.
* REST entities (orders, positions, order book) follow ``/api/v1`` in the packet.

Parsers are tolerant (unknown fields ignored, missing optional fields defaulted) because
the live quote/orderbook field names beyond bid/ask are not fully specified in the packet;
these spots are marked and must be confirmed against a live feed (see docs/EXCHANGE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @classmethod
    def of_order(cls, signed_qty: int) -> Side:
        return cls.BUY if signed_qty > 0 else cls.SELL


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class Phase(str, Enum):
    PRE_STORM = "pre_storm"
    STORM = "storm"
    AFTERMATH = "aftermath"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
@dataclass
class Quote:
    """Top-of-book quote from the ``quotes`` channel."""

    symbol: str
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    time: str | None = None
    received_at: float = 0.0

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return 0.5 * (self.bid + self.ask)

    @classmethod
    def parse(cls, d: dict[str, Any], received_at: float = 0.0) -> Quote:
        return cls(
            symbol=d["symbol"],
            bid=_as_float(d.get("bid")),
            ask=_as_float(d.get("ask")),
            bid_size=_as_float(d.get("bid_size")),
            ask_size=_as_float(d.get("ask_size")),
            time=d.get("time"),
            received_at=received_at,
        )


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """A depth snapshot from ``GET /api/v1/orderbook/{symbol}`` or the orderbook channel."""

    symbol: str
    bids: list[BookLevel] = field(default_factory=list)  # sorted best (highest) first
    asks: list[BookLevel] = field(default_factory=list)  # sorted best (lowest) first
    time: str | None = None
    received_at: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return 0.5 * (self.bids[0].price + self.asks[0].price)

    @classmethod
    def parse(cls, d: dict[str, Any], symbol: str | None = None, received_at: float = 0.0) -> OrderBook:
        def levels(key: str) -> list[BookLevel]:
            out: list[BookLevel] = []
            for lvl in d.get(key, []) or []:
                if isinstance(lvl, dict):
                    price = _as_float(lvl.get("price"))
                    size = _as_float(lvl.get("size") or lvl.get("quantity"))
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    price, size = _as_float(lvl[0]), _as_float(lvl[1])
                else:
                    continue
                if price is not None:
                    out.append(BookLevel(price, size or 0.0))
            return out

        bids = levels("bids")
        asks = levels("asks")
        bids.sort(key=lambda lv: lv.price, reverse=True)
        asks.sort(key=lambda lv: lv.price)
        return cls(
            symbol=symbol or d.get("symbol", ""),
            bids=bids,
            asks=asks,
            time=d.get("time"),
            received_at=received_at,
        )


# ---------------------------------------------------------------------------
# Orders / positions
# ---------------------------------------------------------------------------
@dataclass
class OrderRequest:
    """A proposed order, before any risk check or submission."""

    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.LIMIT
    price: float | None = None
    client_tag: str | None = None  # for dedup / idempotency on our side

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": int(self.quantity),
        }
        if self.order_type is OrderType.LIMIT:
            if self.price is None:
                raise ValueError("limit order requires a price")
            body["price"] = round(float(self.price), 6)  # packet: up to 6 decimals
        return body

    @property
    def signed_qty(self) -> int:
        return self.quantity if self.side is Side.BUY else -self.quantity

    def dedup_key(self) -> tuple[str, str, str, float | None]:
        """Identity used to avoid placing duplicate/near-identical resting orders."""
        return (self.symbol, self.side.value, self.order_type.value, self.price)


@dataclass
class OrderAck:
    """Exchange response to a placed order."""

    order_id: str | None
    symbol: str
    status: str | None
    filled: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, d: dict[str, Any]) -> OrderAck:
        return cls(
            order_id=_as_str(d.get("order_id") or d.get("id")),
            symbol=d.get("symbol", ""),
            status=d.get("status"),
            filled=_as_float(d.get("filled") or d.get("filled_quantity")) or 0.0,
            raw=d,
        )


@dataclass
class OpenOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float | None
    filled: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled)

    @property
    def signed_remaining(self) -> float:
        return self.remaining if self.side is Side.BUY else -self.remaining

    @classmethod
    def parse(cls, d: dict[str, Any]) -> OpenOrder:
        return cls(
            order_id=str(d.get("order_id") or d.get("id") or ""),
            symbol=d.get("symbol", ""),
            side=Side(d.get("side", "buy")),
            quantity=_as_float(d.get("quantity")) or 0.0,
            price=_as_float(d.get("price")),
            filled=_as_float(d.get("filled") or d.get("filled_quantity")) or 0.0,
        )


@dataclass
class Position:
    symbol: str
    quantity: int
    mark: float | None = None
    unrealized_pnl: float | None = None

    @classmethod
    def parse(cls, d: dict[str, Any]) -> Position:
        return cls(
            symbol=d.get("symbol", ""),
            quantity=int(_as_float(d.get("quantity") or d.get("position")) or 0),
            mark=_as_float(d.get("mark") or d.get("mark_price")),
            unrealized_pnl=_as_float(d.get("unrealized_pnl") or d.get("unrealized")),
        )


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    """A parsed market-data WebSocket frame."""

    type: str
    raw: dict[str, Any]
    received_at: float = 0.0

    @property
    def message(self) -> dict[str, Any] | None:
        m = self.raw.get("message")
        return m if isinstance(m, dict) else None

    @classmethod
    def parse(cls, d: dict[str, Any], received_at: float = 0.0) -> Frame:
        return cls(type=d.get("type", ""), raw=d, received_at=received_at)


# Feed message types that the SeasonEstimator understands (carried in news frames).
FEED_MESSAGE_TYPES = {
    "storm",
    "track",
    "storm_formed",
    "storm_dissipated",
    "impact",
    "advisory",
    "damage_report",
    "news",
    "announcement",
    "snapshot",
}


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_str(v: Any) -> str | None:
    return None if v is None else str(v)
