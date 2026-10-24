"""Tests for the bot's status polling, subscriptions, and same-stream recording."""

from __future__ import annotations

import asyncio
import json
import time

from ctc.config import ALL_SYMBOLS
from ctc.execution.automation import AutomaticTransition, SafetySnapshot, TransitionPhase, VerifiedPnLSnapshot
from ctc.execution.client import ExchangeClient
from ctc.execution.models import Frame, OpenOrder, OrderAck, Quote
from ctc.execution.runner import _build_trader, _load_accounting_provider, main, run_bot
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


def test_runner_wires_verified_accounting_provider(monkeypatch, tmp_path):
    client = _FakeClient(None)
    monkeypatch.setattr("ctc.execution.runner.ExchangeClient", lambda _cfg: client)

    async def provider(_client):
        return VerifiedPnLSnapshot(
            starting_equity=2_000_000,
            realized_pnl=0,
            unrealized_pnl=0,
            as_of=time.time(),
            source_verified=True,
            execution_history_complete=True,
        )

    pnl_path = tmp_path / "pnl.json"
    asyncio.run(
        run_bot(
            LiveConfig(
                api_key="unused",
                dry_run=True,
                order_registry_path=str(tmp_path / "registry.json"),
                session_pnl_path=str(pnl_path),
                process_lock_path=str(tmp_path / "bot.lock"),
            ),
            step_interval_s=0.02,
            reconcile_every_s=10.0,
            status_poll_s=0.01,
            recording_path=None,
            accounting_provider=provider,
        )
    )
    assert json.loads(pnl_path.read_text())["source_verified"] is True


def test_accounting_provider_import_syntax_and_callable(monkeypatch):
    import pytest

    with pytest.raises(ValueError, match="module:function"):
        _load_accounting_provider("missing-separator")

    def provider(_client):
        return None

    module = type("AccountingModule", (), {"read": provider})
    monkeypatch.setattr("ctc.execution.runner.importlib.import_module", lambda _name: module)
    assert _load_accounting_provider("team_accounting:read") is provider


class _ExecutionTestClient:
    def __init__(self, *, cancellation_succeeds=True):
        self.orders = []
        self.cancellation_succeeds = cancellation_succeeds
        self.place_calls = 0
        self.cancel_calls = 0

    async def get_open_orders(self):
        return list(self.orders)

    async def get_positions(self):
        return []

    async def place_order(self, request):
        self.place_calls += 1
        self.orders = [
            OpenOrder("port-test-1", request.symbol, request.side, request.quantity, request.price)
        ]
        return OrderAck("port-test-1", request.symbol, "accepted")

    async def cancel_order(self, order_id):
        self.cancel_calls += 1
        if not self.cancellation_succeeds:
            raise RuntimeError("mock cancel failure")
        self.orders = [order for order in self.orders if order.order_id != order_id]


def _execution_test_trader(tmp_path, client):
    cfg = LiveConfig(
        api_key="unused",
        dry_run=False,
        automatic_transition_enabled=True,
        order_registry_path=str(tmp_path / "registry.json"),
        max_initial_gross_exposure=10_000,
    )
    trader = _build_trader(cfg, client)
    trader.state.update_exchange_status({"competition_id": "season-1", "state": "running"})
    trader.state.quotes["PORT"] = Quote("PORT", 80, 81, received_at=time.time())
    assert trader.order_registry.reconcile([])
    trader.last_reconcile_ok = True
    return trader


def test_port_execution_test_records_successful_mocked_round_trip(tmp_path):
    from ctc.execution.runner import _run_port_execution_test

    client = _ExecutionTestClient()
    trader = _execution_test_trader(tmp_path, client)
    evidence = asyncio.run(_run_port_execution_test(trader, timeout_s=0, poll_interval_s=0.01))
    assert evidence is not None and evidence.passed
    assert client.place_calls == client.cancel_calls == 1
    assert trader.order_registry.execution_test_attempted
    assert trader.order_registry.execution_test_result["order_id"] == "port-test-1"


def test_port_execution_test_cancel_failure_is_recorded_as_failed(tmp_path):
    from ctc.execution.runner import _run_port_execution_test

    client = _ExecutionTestClient(cancellation_succeeds=False)
    trader = _execution_test_trader(tmp_path, client)
    evidence = asyncio.run(_run_port_execution_test(trader, timeout_s=0, poll_interval_s=0.01))
    assert evidence is not None and not evidence.passed
    assert client.place_calls == client.cancel_calls == 1
    assert trader.execution_fault


def test_complete_automatic_transition_with_mocked_port_execution(tmp_path):
    from ctc.execution.runner import _run_port_execution_test

    class Clock:
        now = 100.0

        def __call__(self):
            return self.now

    clock = Clock()
    transition = AutomaticTransition(clock=clock, live_activation_enabled=True)
    snapshot = SafetySnapshot(
        exchange_running=True,
        websocket_healthy=True,
        fresh_symbols=frozenset(ALL_SYMBOLS),
        execution_api_healthy=True,
        positive_net_edge=True,
        limits_enforced=True,
        pnl_complete=True,
        loss_limit_clear=True,
        ownership_verified=True,
        executions_complete=True,
        process_healthy=True,
        no_unexplained_discrepancy=True,
    )
    transition.tick(snapshot, observe=True)
    for _ in range(7):
        clock.now += 5
        transition.tick(snapshot, observe=True)
    clock.now += 5
    assert transition.tick(snapshot, observe=True) is TransitionPhase.EXECUTION_TEST

    trader = _execution_test_trader(tmp_path, _ExecutionTestClient())
    evidence = asyncio.run(_run_port_execution_test(trader, timeout_s=0, poll_interval_s=0.01))
    assert evidence is not None and evidence.passed
    transition.record_execution_test(evidence)
    assert transition.tick(snapshot) is TransitionPhase.READY_FOR_LIVE
    assert transition.tick(snapshot) is TransitionPhase.LIVE


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


def test_unarmed_startup_still_enforces_initial_caps(tmp_path):
    cfg = LiveConfig(
        dry_run=True,
        automatic_transition_enabled=False,
        order_registry_path=str(tmp_path / "registry.json"),
    )
    client = ExchangeClient(cfg, transport=lambda *_args: (200, "{}"))
    trader = _build_trader(cfg, client)
    assert trader.risk.max_order_size_frac == 0.01
    assert trader.risk.max_gross_exposure == 10_000
    assert trader.risk.max_order_size("PORT") == 1
    assert client.order_limiter.max_events == 5


def test_port_test_reduces_inherited_long_without_bypassing_gross_cap(tmp_path):
    from ctc.execution.models import Position
    from ctc.execution.runner import _run_port_execution_test

    class Client(_ExecutionTestClient):
        async def get_positions(self):
            return [Position("PORT", 120)]

    client = Client()
    trader = _execution_test_trader(tmp_path, client)
    trader.state.positions["PORT"] = 120
    trader.risk.max_gross_exposure = 1000
    evidence = asyncio.run(_run_port_execution_test(trader, timeout_s=0))
    assert evidence.passed and evidence.side == "sell"
    assert evidence.limit_price > evidence.best_ask_before


def test_quiet_quotes_are_confirmed_from_actual_rest_books():
    from ctc.execution.models import BookLevel, OrderBook
    from ctc.execution.runner import _refresh_stale_quotes

    class Client:
        async def get_orderbook(self, symbol):
            return OrderBook(symbol, [BookLevel(10, 1)], [BookLevel(11, 1)])

    state = _build_trader(LiveConfig(), None).state
    asyncio.run(_refresh_stale_quotes(Client(), state, 10))
    assert len(state.quotes) == len(ALL_SYMBOLS)
    assert state.quotes["PORT"].bid == 10
    assert state.stale_symbols(10) == []
