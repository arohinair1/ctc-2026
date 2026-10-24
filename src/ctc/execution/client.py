"""Asynchronous Archipelago Exchange client (REST + market-data WebSocket).

Protocol per the case packet appendix and starter kit:

* REST under ``/api/v1`` (orders, positions, orderbook, competitions/time, ETF
  create/redeem); the static case is ``GET /cases/hurricane.json`` (no ``/api/v1``).
* Every request sends ``X-API-Key`` and a non-default ``User-Agent`` (the exchange's
  front door refuses urllib's default agent).
* Market data over ``wss://.../ws/v1/market-data``: send exactly one ``subscribe`` frame
  within 10 s of connecting; each frame is one message.

Design for testability: HTTP goes through an injectable ``transport`` (a sync callable run
in a worker thread; defaults to :func:`urllib_transport`), and the WebSocket through an
injectable ``ws_connect`` (defaults to :func:`websockets.connect`). Tests pass fakes and
never touch the network. Order actions pass through an async rate limiter kept safely below
the exchange's 60/min/bot ceiling.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from ctc.execution.log import get_logger, redact
from ctc.execution.models import (
    Frame,
    OpenOrder,
    OrderAck,
    OrderBook,
    OrderRequest,
    Position,
)
from ctc.execution.settings import LiveConfig

log = get_logger("client")

# A sync HTTP transport: (method, url, headers, body_bytes, timeout) -> (status, text)
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, str]]


class ExchangeError(RuntimeError):
    """Non-2xx HTTP response from the exchange."""

    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"HTTP {status} for {path}: {body[:200]}")
        self.status = status
        self.body = body
        self.path = path


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, str]:
    """Default stdlib transport (blocking; run via ``asyncio.to_thread``)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class RateLimiter:
    """Sliding-window limiter: at most ``max_events`` in ``window_s`` seconds."""

    def __init__(self, max_events: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.max_events = max_events
        self.window_s = window_s
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            while self._events and now - self._events[0] >= self.window_s:
                self._events.popleft()
            if len(self._events) >= self.max_events:
                wait = self.window_s - (now - self._events[0])
                log.warning("rate limit %d/%ds reached; waiting %.1fs", self.max_events, self.window_s, wait)
                await asyncio.sleep(max(0.0, wait))
                return await self.acquire()
            self._events.append(self._clock())

    def would_exceed(self) -> bool:
        now = self._clock()
        while self._events and now - self._events[0] >= self.window_s:
            self._events.popleft()
        return len(self._events) >= self.max_events


class ExchangeClient:
    def __init__(
        self,
        config: LiveConfig,
        *,
        transport: Transport | None = None,
        ws_connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = config
        self._transport = transport or urllib_transport
        self._ws_connect = ws_connect
        self._clock = clock
        action_limit = min(config.max_orders_per_min, config.initial_orders_per_min)
        if not config.automatic_transition_enabled:
            action_limit = config.max_orders_per_min
        self.order_limiter = RateLimiter(action_limit, 60.0, clock)

    # ---- headers -------------------------------------------------------
    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {"User-Agent": self.cfg.user_agent, "X-API-Key": self.cfg.api_key}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    # ---- generic request ----------------------------------------------
    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, api: bool = True
    ) -> Any:
        url = self.cfg.http_url + ("/api/v1" if api else "") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers(json_body=body is not None)
        status, text = await asyncio.to_thread(
            self._transport, method, url, headers, data, self.cfg.request_timeout_s
        )
        if not (200 <= status < 300):
            raise ExchangeError(status, redact(text, self.cfg.api_key), path)
        return json.loads(text) if text else {}

    # ---- read endpoints ------------------------------------------------
    async def get_case(self) -> dict[str, Any]:
        return await self._request("GET", "/cases/hurricane.json", api=False)

    async def get_time(self) -> dict[str, Any]:
        return await self._request("GET", "/competitions/time")

    async def get_positions(self) -> list[Position]:
        data = await self._request("GET", "/positions")
        rows = data.get("positions", data) if isinstance(data, dict) else data
        return [Position.parse(r) for r in rows] if isinstance(rows, list) else []

    async def get_open_orders(self) -> list[OpenOrder]:
        data = await self._request("GET", "/orders/open")
        rows = data.get("orders", data) if isinstance(data, dict) else data
        return [OpenOrder.parse(r) for r in rows] if isinstance(rows, list) else []

    async def get_orderbook(self, symbol: str) -> OrderBook:
        data = await self._request("GET", f"/orderbook/{symbol}")
        return OrderBook.parse(data, symbol=symbol, received_at=time.time())

    # ---- write endpoints (rate-limited) --------------------------------
    async def place_order(self, req: OrderRequest) -> OrderAck:
        await self.order_limiter.acquire()
        data = await self._request("POST", "/orders", req.to_payload())
        return OrderAck.parse(data if isinstance(data, dict) else {})

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        await self.order_limiter.acquire()
        return await self._request("DELETE", f"/orders/{order_id}")

    async def cancel_all(self, symbol: str | None = None) -> dict[str, Any]:
        await self.order_limiter.acquire()
        path = "/orders/all" + (f"?symbol={symbol}" if symbol else "")
        return await self._request("DELETE", path)

    async def etf_create(self, symbol: str, quantity: int) -> dict[str, Any]:
        await self.order_limiter.acquire()
        return await self._request("POST", f"/etf/{symbol}/create", {"quantity": int(quantity)})

    async def etf_redeem(self, symbol: str, quantity: int) -> dict[str, Any]:
        await self.order_limiter.acquire()
        return await self._request("POST", f"/etf/{symbol}/redeem", {"quantity": int(quantity)})

    # ---- market-data stream -------------------------------------------
    async def stream(
        self,
        symbols: Iterable[str],
        channels: Iterable[str] = ("news",),
        *,
        max_reconnects: int | None = None,
    ) -> AsyncIterator[Frame]:
        """Yield parsed frames, reconnecting with exponential backoff.

        Re-sends the subscription on every (re)connect. A sentinel frame of type
        ``"_reconnect"`` is yielded after a drop so callers can trigger REST resync.
        """
        if self._ws_connect is None:
            import websockets  # imported lazily so the package imports without the extra

            self._ws_connect = websockets.connect

        sym_list = list(symbols)
        chan_list = list(channels)
        subscribe = json.dumps({"action": "subscribe", "symbols": sym_list, "channels": chan_list})
        backoff = 1.0
        attempts = 0
        while True:
            try:
                async with self._ws_connect(
                    self.cfg.ws_url,
                    additional_headers={"X-API-Key": self.cfg.api_key, "User-Agent": self.cfg.user_agent},
                ) as ws:
                    await ws.send(subscribe)
                    log.info("ws connected; channels=%s symbols=%d", chan_list, len(sym_list))
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                        except (ValueError, TypeError):
                            log.warning("dropping non-JSON frame")
                            continue
                        yield Frame.parse(d, received_at=time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surface any transport error as a reconnect
                log.warning("ws disconnected (%s); reconnecting in %.0fs", type(exc).__name__, backoff)
                yield Frame(type="_reconnect", raw={"error": type(exc).__name__}, received_at=time.time())
            attempts += 1
            if max_reconnects is not None and attempts > max_reconnects:
                log.error("giving up after %d reconnect attempts", attempts)
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.cfg.ws_max_backoff_s)

    # ---- connectivity probe -------------------------------------------
    async def probe(self) -> dict[str, Any]:
        """Read-only connectivity check. Never places orders. Returns a result dict."""
        result: dict[str, Any] = {"public_case": None, "auth_time": None, "auth_positions": None}
        try:
            case = await self.get_case()
            result["public_case"] = {"ok": True, "symbols": len(case.get("limits", {}))}
        except Exception as exc:  # noqa: BLE001
            result["public_case"] = {"ok": False, "error": _err(exc)}
        for label, coro in (("auth_time", self.get_time), ("auth_positions", self.get_positions)):
            try:
                await coro()
                result[label] = {"ok": True}
            except Exception as exc:  # noqa: BLE001
                result[label] = {"ok": False, "error": _err(exc)}
        return result


def _err(exc: Exception) -> str:
    if isinstance(exc, ExchangeError):
        return f"HTTP {exc.status}: {exc.body[:120]}"
    return f"{type(exc).__name__}: {str(exc)[:120]}"
