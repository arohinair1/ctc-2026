"""Tests for the bot's status polling, subscriptions, and same-stream recording."""

from __future__ import annotations

import asyncio
import json

from ctc.execution.client import ExchangeClient
from ctc.execution.models import Frame
from ctc.execution.runner import _build_trader, main, run_bot
from ctc.execution.settings import LiveConfig


class _FakeClient:
    def __init__(self, _cfg):
        self.time_calls = 0
        self.channels = None

    async def get_time(self):
        self.time_calls += 1
        return {"competition_id": "season-1", "state": "ready", "fictional_time": "09:30"}

    async def get_positions(self):
        return []

    async def get_open_orders(self):
        return []

    async def stream(self, symbols, channels):
        assert symbols
        self.channels = list(channels)
        yield Frame(
            type="quote",
            raw={"type": "quote", "symbol": "PORT", "bid": 70.0, "ask": 71.0},
            received_at=123.0,
        )
        await asyncio.sleep(0.04)


def test_runner_polls_status_and_records_single_stream(monkeypatch, tmp_path):
    client = _FakeClient(None)
    monkeypatch.setattr("ctc.execution.runner.ExchangeClient", lambda _cfg: client)

    asyncio.run(
        run_bot(
            LiveConfig(
                api_key="unused",
                dry_run=True,
                order_registry_path=str(tmp_path / "registry.json"),
                session_pnl_path=str(tmp_path / "pnl.json"),
                process_lock_path=str(tmp_path / "bot.lock"),
            ),
            step_interval_s=0.02,
            reconcile_every_s=10.0,
            status_poll_s=0.01,
            recording_path=tmp_path / "feed.jsonl",
        )
    )

    assert client.time_calls >= 2
    assert client.channels == ["news", "quotes", "trades", "orderbook"]
    recorded = [json.loads(line) for line in (tmp_path / "feed.jsonl").read_text().splitlines()]
    assert recorded == [{"type": "quote", "symbol": "PORT", "bid": 70.0, "ask": 71.0, "received_at": 123.0}]


def test_live_mode_requires_both_environment_and_cli_gates(monkeypatch):
    monkeypatch.setattr("ctc.execution.runner.setup_logging", lambda: None)
    config = LiveConfig(dry_run=False)
    monkeypatch.setattr("ctc.execution.runner.LiveConfig.from_env", lambda: config)
    monkeypatch.setattr("sys.argv", ["ctc-bot"])

    assert main() == 2

    config.dry_run = True
    monkeypatch.setattr("sys.argv", ["ctc-bot", "--live"])
    assert main() == 2


def test_live_mode_also_requires_automatic_transition_gate(monkeypatch):
    monkeypatch.setattr("ctc.execution.runner.setup_logging", lambda: None)
    cfg = LiveConfig(dry_run=False, automatic_transition_enabled=False)
    monkeypatch.setattr("ctc.execution.runner.LiveConfig.from_env", lambda: cfg)
    monkeypatch.setattr("sys.argv", ["ctc-bot", "--live"])
    assert main() == 2


def test_armed_rollout_uses_smaller_initial_caps(tmp_path):
    cfg = LiveConfig(
        dry_run=True,
        automatic_transition_enabled=True,
        order_registry_path=str(tmp_path / "registry.json"),
    )
    client = ExchangeClient(cfg, transport=lambda *_args: (200, "{}"))
    trader = _build_trader(cfg, client)
    assert trader.risk.max_order_size_frac == 0.01
    assert trader.risk.max_gross_exposure == 10_000
    assert trader.risk.max_order_size("PORT") == 1
    assert client.order_limiter.max_events == 5
