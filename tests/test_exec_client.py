"""Tests for the async exchange client (REST + WS) with injected fakes — no network."""

from __future__ import annotations

import asyncio
import json

import pytest

from ctc.execution.client import ExchangeClient, ExchangeError, RateLimiter
from ctc.execution.models import OrderRequest, OrderType, Side
from ctc.execution.settings import LiveConfig


def _cfg(**kw):
    return LiveConfig(api_key="SECRET_KEY_123", **kw)


def make_transport(responses):
    """responses: list of (status, text). Records calls on ``.calls``."""
    queue = list(responses)

    def transport(method, url, headers, body, timeout):
        transport.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return queue.pop(0) if queue else (200, "{}")

    transport.calls = []
    return transport


# ---- REST ---------------------------------------------------------------
def test_get_positions_parses_list():
    t = make_transport([(200, json.dumps({"positions": [{"symbol": "PORT", "quantity": -3}]}))])
    client = ExchangeClient(_cfg(), transport=t)
    positions = asyncio.run(client.get_positions())
    assert len(positions) == 1 and positions[0].quantity == -3


def test_place_order_sends_payload_and_parses_ack():
    t = make_transport([(200, json.dumps({"order_id": "o9", "symbol": "PORT", "status": "accepted"}))])
    client = ExchangeClient(_cfg(), transport=t)
    req = OrderRequest("PORT", Side.SELL, 5, OrderType.LIMIT, price=70.0)
    ack = asyncio.run(client.place_order(req))
    assert ack.order_id == "o9" and ack.status == "accepted"
    call = t.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/api/v1/orders")
    assert json.loads(call["body"]) == req.to_payload()


def test_headers_include_key_and_custom_user_agent():
    t = make_transport([(200, "{}")])
    client = ExchangeClient(_cfg(), transport=t)
    asyncio.run(client.get_time())
    h = t.calls[0]["headers"]
    assert h["X-API-Key"] == "SECRET_KEY_123"
    assert "ctc-2026-bot" in h["User-Agent"]  # non-default UA (exchange refuses urllib default)


def test_public_case_has_no_api_prefix():
    t = make_transport([(200, json.dumps({"limits": {"PORT": 200}}))])
    client = ExchangeClient(_cfg(), transport=t)
    asyncio.run(client.get_case())
    assert t.calls[0]["url"].endswith("/cases/hurricane.json")
    assert "/api/v1" not in t.calls[0]["url"]


def test_error_status_raises_and_redacts_key():
    t = make_transport([(401, "Invalid API key SECRET_KEY_123 rejected")])
    client = ExchangeClient(_cfg(), transport=t)
    with pytest.raises(ExchangeError) as ei:
        asyncio.run(client.get_positions())
    assert ei.value.status == 401
    assert "SECRET_KEY_123" not in str(ei.value)  # redacted in message/body


# ---- rate limiter -------------------------------------------------------
def test_rate_limiter_window():
    now = [0.0]
    rl = RateLimiter(max_events=2, window_s=60.0, clock=lambda: now[0])
    asyncio.run(rl.acquire())
    asyncio.run(rl.acquire())
    assert rl.would_exceed() is True
    now[0] = 61.0  # events age out of the window
    assert rl.would_exceed() is False


# ---- WebSocket stream (fake ws_connect) ---------------------------------
class _FakeWS:
    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


class _FakeConnect:
    def __init__(self, messages=None, raise_on_enter=False):
        self.messages = messages or []
        self.raise_on_enter = raise_on_enter
        self.ws = None

    def __call__(self, url, additional_headers=None):
        self.url = url
        self.headers = additional_headers
        return self

    async def __aenter__(self):
        if self.raise_on_enter:
            raise ConnectionError("boom")
        self.ws = _FakeWS(self.messages)
        return self.ws

    async def __aexit__(self, *exc):
        return False


async def _collect(aiter):
    return [f async for f in aiter]


def test_stream_yields_frames_and_sends_subscription():
    msgs = [json.dumps({"type": "quote", "symbol": "PORT", "bid": 1.0, "ask": 2.0})]
    fake = _FakeConnect(messages=msgs)
    client = ExchangeClient(_cfg(), ws_connect=fake)
    frames = asyncio.run(_collect(client.stream(["PORT"], ["quotes"], max_reconnects=0)))
    assert [f.type for f in frames] == ["quote"]
    # exactly one subscribe frame, correct shape
    sub = json.loads(fake.ws.sent[0])
    assert sub == {"action": "subscribe", "symbols": ["PORT"], "channels": ["quotes"]}


def test_stream_emits_reconnect_sentinel_on_failure():
    fake = _FakeConnect(raise_on_enter=True)
    client = ExchangeClient(_cfg(), ws_connect=fake)
    frames = asyncio.run(_collect(client.stream(["PORT"], ["news"], max_reconnects=0)))
    assert [f.type for f in frames] == ["_reconnect"]


def test_limiter_resumes_after_full_window_without_recursive_lock_deadlock():
    async def scenario():
        limiter = RateLimiter(1, window_s=0.01)
        await limiter.acquire()
        await asyncio.wait_for(limiter.acquire(), timeout=0.2)

    asyncio.run(scenario())
