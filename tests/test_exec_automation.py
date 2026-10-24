"""Restart, ownership, PnL and staged transition safety tests."""

from __future__ import annotations

import pytest

from ctc.config import ALL_SYMBOLS
from ctc.execution.automation import (
    AutomaticTransition,
    BotOrderRegistry,
    ExecutionTestEvidence,
    OrderRegistryError,
    ProcessLease,
    SafetySnapshot,
    SessionPnLTracker,
    TransitionPhase,
)
from ctc.execution.models import OpenOrder, OrderAck, OrderRequest, OrderType, Side


def _request() -> OrderRequest:
    return OrderRequest("PORT", Side.BUY, 3, OrderType.LIMIT, 40.0, "test-tag")


def test_registry_persists_owned_id_and_partial_execution(tmp_path):
    path = tmp_path / "registry.json"
    registry = BotOrderRegistry(path)
    intent = registry.begin_submission(_request())
    registry.record_ack(intent, OrderAck("ord-1", "PORT", "accepted"))
    registry.record_execution("ord-1", "fill-1", 1, 39.5)
    registry.record_execution("ord-1", "fill-1", 1, 39.5)  # idempotent replay

    restored = BotOrderRegistry(path)
    order = restored.orders["ord-1"]
    assert order.status == "partially_filled"
    assert order.filled_quantity == 1
    assert order.executions == [{"execution_id": "fill-1", "quantity": 1.0, "price": 39.5}]
    assert restored.ownership_verified is False  # restart always requires reconciliation
    assert restored.reconcile([OpenOrder("ord-1", "PORT", Side.BUY, 3, 40.0, filled=1)])
    assert restored.known_open_ids() == ("ord-1",)
    assert restored.execution_records_complete


def test_registry_does_not_claim_fill_accounting_from_quantity_only():
    registry = BotOrderRegistry()
    intent = registry.begin_submission(_request())
    registry.record_ack(intent, OrderAck("ord-1", "PORT", "accepted"))
    registry.reconcile([OpenOrder("ord-1", "PORT", Side.BUY, 3, 40.0, filled=1)])
    assert registry.ownership_verified
    assert not registry.execution_records_complete


def test_registry_rejects_unowned_order_and_unknown_cancel(tmp_path):
    registry = BotOrderRegistry(tmp_path / "registry.json")
    assert not registry.reconcile([OpenOrder("manual", "PORT", Side.BUY, 1, 1.0)])
    assert not registry.ownership_verified
    with pytest.raises(OrderRegistryError, match="not reconciled"):
        registry.known_open_ids()
    with pytest.raises(OrderRegistryError, match="unowned"):
        registry.record_cancel("manual", confirmed=True)


def test_registry_marks_disappearing_order_unresolved(tmp_path):
    registry = BotOrderRegistry(tmp_path / "registry.json")
    intent = registry.begin_submission(_request())
    registry.record_ack(intent, OrderAck("ord-1", "PORT", "accepted"))
    assert registry.reconcile([OpenOrder("ord-1", "PORT", Side.BUY, 3, 40.0)])
    assert not registry.reconcile([])
    assert "ord-1" in registry.unresolved


def test_registry_handles_cancellation_and_rejects_overfill(tmp_path):
    registry = BotOrderRegistry(tmp_path / "registry.json")
    intent = registry.begin_submission(_request())
    registry.record_ack(intent, OrderAck("ord-1", "PORT", "accepted"))
    with pytest.raises(OrderRegistryError, match="exceeds"):
        registry.record_execution("ord-1", "fill-too-large", 4, 40.0)
    registry.record_execution("ord-1", "fill-1", 1, 39.0)
    registry.record_cancel("ord-1", confirmed=True)
    restored = BotOrderRegistry(tmp_path / "registry.json")
    assert restored.orders["ord-1"].status == "cancelled"
    assert restored.orders["ord-1"].filled_quantity == 1


def test_unreadable_registry_fails_closed(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("not-json")
    with pytest.raises(OrderRegistryError, match="unreadable"):
        BotOrderRegistry(path)


def test_process_lease_rejects_a_second_bot_instance(tmp_path):
    first = ProcessLease(tmp_path / "bot.lock")
    second = ProcessLease(tmp_path / "bot.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_ambiguous_submission_survives_restart_as_unresolved(tmp_path):
    path = tmp_path / "registry.json"
    registry = BotOrderRegistry(path)
    intent = registry.begin_submission(_request())
    registry.record_failure(intent, definite_rejection=False)
    restored = BotOrderRegistry(path)
    assert not restored.ownership_verified
    assert intent in restored.unresolved


def test_definite_rejection_does_not_leave_an_order(tmp_path):
    registry = BotOrderRegistry(tmp_path / "registry.json")
    intent = registry.begin_submission(_request())
    registry.record_failure(intent, definite_rejection=True)
    assert registry.reconcile([])


def test_pnl_missing_or_invalid_data_is_incomplete(tmp_path):
    tracker = SessionPnLTracker(tmp_path / "pnl.json", clock=lambda: 100.0)
    assert not tracker.update(
        starting_equity=100_000, realized_pnl=None, unrealized_pnl=0, source_verified=True
    )
    assert not tracker.complete
    assert tracker.session_pnl is None
    assert not tracker.update(
        starting_equity=float("nan"), realized_pnl=0, unrealized_pnl=0, source_verified=True
    )


def test_pnl_persistence_restart_requires_fresh_reverification(tmp_path):
    path = tmp_path / "pnl.json"
    tracker = SessionPnLTracker(path, clock=lambda: 100.0)
    assert tracker.update(starting_equity=100_000, realized_pnl=25, unrealized_pnl=-10, source_verified=True)
    assert tracker.complete
    restored = SessionPnLTracker(path, clock=lambda: 101.0)
    assert not restored.complete
    assert restored.update(starting_equity=100_000, realized_pnl=25, unrealized_pnl=-10, source_verified=True)
    assert restored.session_pnl == 0


def test_stale_pnl_snapshot_is_not_complete():
    clock = _Clock()
    tracker = SessionPnLTracker(clock=clock, max_age_s=10)
    tracker.update(starting_equity=500_000, realized_pnl=0, unrealized_pnl=0, source_verified=True)
    clock.now += 10.01
    assert not tracker.complete
    assert tracker.session_pnl is None


def test_pnl_loss_stop_uses_realized_plus_unrealized_and_latches(tmp_path):
    tracker = SessionPnLTracker(tmp_path / "pnl.json", loss_limit=10_000, clock=lambda: 10)
    tracker.update(starting_equity=2_000_000, realized_pnl=100, unrealized_pnl=500, source_verified=True)
    tracker.update(
        starting_equity=2_000_000, realized_pnl=-8_000, unrealized_pnl=-2_100, source_verified=True
    )
    assert tracker.session_pnl == -10_700
    assert tracker.loss_limit_reached
    tracker.update(starting_equity=2_000_000, realized_pnl=-1_000, unrealized_pnl=0, source_verified=True)
    assert tracker.loss_limit_reached  # recovery does not auto-resume
    with pytest.raises(PermissionError):
        tracker.authorize_resume(manual_authorization=False)
    tracker.authorize_resume(manual_authorization=True)
    assert not tracker.loss_limit_reached
    assert tracker.session_pnl == 0


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _snapshot(**overrides):
    values = dict(
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
    values.update(overrides)
    return SafetySnapshot(**values)


def _test_evidence(**overrides):
    values = dict(
        order_id="test-order-1",
        symbol="PORT",
        side="buy",
        quantity=1,
        limit_price=40,
        best_bid_before=80,
        fill_quantity=0,
        appeared_in_open_orders=True,
        cancellation_acknowledged=True,
        absent_after_cancel=True,
        port_position_before=0,
        port_position_after=0,
    )
    values.update(overrides)
    return ExecutionTestEvidence(**values)


def _reach_execution_test(machine, clock):
    machine.tick(_snapshot(), observe=True)  # open -> observing
    for _ in range(7):
        machine.tick(_snapshot(), observe=True)
        clock.now += 5
    machine.tick(_snapshot(), observe=True)
    assert machine.phase is TransitionPhase.EXECUTION_TEST


def test_transition_observes_30_seconds_then_waits_for_test_and_live_gates():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock, live_activation_enabled=False)
    _reach_execution_test(machine, clock)
    machine.record_execution_test(_test_evidence())
    assert machine.tick(_snapshot()) is TransitionPhase.READY_FOR_LIVE
    assert machine.tick(_snapshot()) is TransitionPhase.READY_FOR_LIVE
    assert machine.phase is not TransitionPhase.LIVE


def test_transition_never_advances_on_missing_gates_or_stale_symbols():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock, live_activation_enabled=True)
    machine.tick(_snapshot(), observe=True)
    for _ in range(8):
        machine.tick(_snapshot(fresh_symbols=frozenset(ALL_SYMBOLS[:-1])), observe=True)
        clock.now += 5
    assert machine.phase is TransitionPhase.OBSERVING
    assert machine.observation_samples == 0


def test_transition_gate_failure_and_manual_resume():
    machine = AutomaticTransition(clock=_Clock(), live_activation_enabled=True)
    machine.tick(_snapshot(process_healthy=False))
    assert machine.phase is TransitionPhase.SAFE_MODE
    with pytest.raises(PermissionError):
        machine.authorize_resume(manual_authorization=False)
    machine.authorize_resume(manual_authorization=True)
    assert machine.phase is TransitionPhase.WAITING_FOR_OPEN


def test_halt_during_observation_resets_without_stopping_monitoring():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock)
    machine.tick(_snapshot(), observe=True)
    assert machine.phase is TransitionPhase.OBSERVING
    assert machine.tick(_snapshot(exchange_running=False)) is TransitionPhase.WAITING_FOR_OPEN


def test_execution_test_failure_enters_safe_mode():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock)
    _reach_execution_test(machine, clock)
    machine.record_execution_test(_test_evidence(absent_after_cancel=False))
    assert machine.phase is TransitionPhase.SAFE_MODE


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_id": ""},
        {"quantity": 2},
        {"limit_price": 80},
        {"fill_quantity": 1},
        {"appeared_in_open_orders": False},
        {"cancellation_acknowledged": False},
        {"port_position_after": 1},
    ],
)
def test_execution_test_requires_complete_unfilled_round_trip_evidence(overrides):
    assert not _test_evidence(**overrides).passed


def test_missing_pnl_or_order_ownership_prevents_live_transition():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock, live_activation_enabled=True)
    _reach_execution_test(machine, clock)
    machine.record_execution_test(_test_evidence())
    assert machine.tick(_snapshot(pnl_complete=False)) is TransitionPhase.EXECUTION_TEST
    assert machine.tick(_snapshot(ownership_verified=False)) is TransitionPhase.EXECUTION_TEST
    assert machine.tick(_snapshot(executions_complete=False)) is TransitionPhase.EXECUTION_TEST


def test_no_edge_keeps_monitoring_without_entering_safe_mode():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock)
    machine.tick(_snapshot(positive_net_edge=False), observe=True)
    for _ in range(7):
        machine.tick(_snapshot(positive_net_edge=False), observe=True)
        clock.now += 5
    assert machine.phase is TransitionPhase.OBSERVING
    assert machine.phase is not TransitionPhase.SAFE_MODE


def test_verified_gates_can_transition_when_explicitly_enabled():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock, live_activation_enabled=True)
    _reach_execution_test(machine, clock)
    machine.record_execution_test(_test_evidence())
    assert machine.tick(_snapshot()) is TransitionPhase.READY_FOR_LIVE
    assert machine.tick(_snapshot()) is TransitionPhase.LIVE


def test_no_edge_during_live_session_does_not_halt_the_bot():
    clock = _Clock()
    machine = AutomaticTransition(clock=clock, live_activation_enabled=True)
    _reach_execution_test(machine, clock)
    machine.record_execution_test(_test_evidence())
    machine.tick(_snapshot())
    machine.tick(_snapshot())
    assert machine.phase is TransitionPhase.LIVE
    assert machine.tick(_snapshot(positive_net_edge=False)) is TransitionPhase.LIVE
