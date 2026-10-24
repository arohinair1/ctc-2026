"""Regression checks for live-verified PnL fields, order history and manual orders."""

from dataclasses import replace

import pytest

from ctc.execution.accounting import parse_account_snapshot
from ctc.execution.automation import BotOrderRegistry, SessionPnLTracker, VerifiedOrderState
from ctc.execution.models import OpenOrder, OrderAck, OrderRequest, Side


def test_closed_positions_retain_realized_pnl_without_inventing_equity():
    snap = parse_account_snapshot(
        {
            "positions": [
                {"symbol": "PORT", "quantity": 0, "realized_pnl": -50, "unrealized_pnl": 0},
                {"symbol": "GRID", "quantity": -2, "realized_pnl": 10, "unrealized_pnl": 3},
            ]
        },
        {"orders": []},
        as_of=100,
    )
    assert snap.starting_equity is None
    assert snap.realized_pnl == -40 and snap.unrealized_pnl == 3
    tracker = SessionPnLTracker(clock=lambda: 100)
    assert tracker.update(
        starting_equity=None,
        realized_pnl=-40,
        unrealized_pnl=3,
        source_verified=True,
        account_pnl_complete=True,
    )
    assert tracker.complete and tracker.session_pnl == 0
    tracker.update(
        starting_equity=None,
        realized_pnl=-10040,
        unrealized_pnl=3,
        source_verified=True,
        account_pnl_complete=True,
    )
    assert tracker.loss_limit_reached


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "10", True])
def test_missing_or_invalid_pnl_never_becomes_zero(value):
    with pytest.raises((KeyError, ValueError)):
        parse_account_snapshot(
            {"positions": [{"symbol": "PORT", "quantity": 0, "realized_pnl": value, "unrealized_pnl": 0}]},
            {"orders": []},
            as_of=100,
        )


def test_manual_order_is_preserved_but_never_owned_or_cancelled(tmp_path):
    path = tmp_path / "orders.json"
    registry = BotOrderRegistry(path)
    manual = OpenOrder("manual", "PORT", Side.BUY, 3, 50)
    with pytest.raises(PermissionError):
        registry.preserve_manual_orders([manual], manual_authorization=False)
    registry.preserve_manual_orders([manual], manual_authorization=True)
    assert registry.reconcile([manual])
    assert registry.known_open_ids() == ()
    registry = BotOrderRegistry(path)
    assert registry.reconcile([manual])
    assert not registry.reconcile([manual, replace(manual, order_id="unexpected")])


def test_bot_fill_reconciles_from_order_status_without_fabricated_fill_price():
    registry = BotOrderRegistry()
    intent = registry.begin_submission(OrderRequest("PORT", Side.BUY, 2, price=70))
    registry.record_ack(intent, OrderAck("bot", "PORT", "accepted"))
    state = VerifiedOrderState("bot", "PORT", "buy", 2, 2, "filled")
    assert not registry.reconcile_order_states(())  # missing/truncated history
    assert not registry.reconcile_order_states((replace(state, filled=1),))
    assert not registry.reconcile_order_states((replace(state, side="sell"),))
    assert registry.reconcile_order_states((state,))
    assert registry.reconcile([])
    assert registry.execution_records_complete
    assert registry.orders["bot"].executions == []


def test_pnl_only_baseline_survives_restart_and_loss_latch(tmp_path):
    path = tmp_path / "pnl.json"
    t = SessionPnLTracker(path, clock=lambda: 100)
    t.update(
        starting_equity=None,
        realized_pnl=100,
        unrealized_pnl=10,
        source_verified=True,
        account_pnl_complete=True,
    )
    t = SessionPnLTracker(path, clock=lambda: 100)
    assert not t.complete
    assert t.update(
        starting_equity=None,
        realized_pnl=-10000,
        unrealized_pnl=0,
        source_verified=True,
        account_pnl_complete=True,
    )
    assert t.session_pnl == -10110 and t.loss_limit_reached
    t.authorize_resume(manual_authorization=True)
    assert t.complete and t.session_pnl == 0


def test_flat_api_null_unrealized_has_exactly_zero_exposure():
    snap = parse_account_snapshot(
        {"positions": [{"symbol": "REIN", "quantity": 0, "realized_pnl": 12, "unrealized_pnl": None}]},
        {"orders": []},
        as_of=100,
    )
    assert snap.unrealized_pnl == 0 and snap.realized_pnl == 12


def test_inherited_excess_exposure_can_be_reduced_but_not_increased():
    from ctc.execution.risk import RiskManager

    risk = RiskManager(max_gross_exposure=10000, max_order_size_units=1)
    args = dict(
        position=120, resting_signed=0, mid=60, gross_exposure=23000, stale=False, existing_dedup_keys=set()
    )
    assert risk.evaluate(OrderRequest("PORT", Side.SELL, 1, price=60), **args).approved
    assert not risk.evaluate(OrderRequest("PORT", Side.BUY, 1, price=60), **args).approved


def test_pending_order_disappearance_is_resolved_only_by_verified_terminal_history():
    registry = BotOrderRegistry()
    intent = registry.begin_submission(OrderRequest("FERRY", Side.BUY, 1, price=18.54))
    registry.record_ack(intent, OrderAck("ours", "FERRY", "pending"))
    assert not registry.reconcile([])
    assert "ours" in registry.unresolved
    active = VerifiedOrderState("ours", "FERRY", "buy", 1, 0, "pending")
    assert not registry.reconcile_order_states((active,))
    assert "ours" in registry.unresolved
    terminal = replace(active, filled=1, status="filled")
    assert not registry.reconcile_order_states((replace(terminal, side="sell"),))
    assert "ours" in registry.unresolved
    assert registry.reconcile_order_states((terminal,))
    assert registry.reconcile([])
    assert registry.ownership_verified


def test_verified_fill_does_not_clear_unrelated_ambiguous_submission():
    registry = BotOrderRegistry()
    intent = registry.begin_submission(OrderRequest("FERRY", Side.BUY, 1, price=18))
    registry.record_ack(intent, OrderAck("ours", "FERRY", "pending"))
    registry.unresolved.add("unknown-intent")
    assert not registry.reconcile_order_states((VerifiedOrderState("ours", "FERRY", "buy", 1, 1, "filled"),))
    assert "unknown-intent" in registry.unresolved


def test_invalid_later_order_prevents_partial_history_latch_resolution():
    registry = BotOrderRegistry()
    for oid in ("first", "second"):
        intent = registry.begin_submission(OrderRequest("FERRY", Side.BUY, 1, price=18))
        registry.record_ack(intent, OrderAck(oid, "FERRY", "pending"))
    assert not registry.reconcile([])
    first = VerifiedOrderState("first", "FERRY", "buy", 1, 1, "filled")
    wrong_second = VerifiedOrderState("second", "FERRY", "sell", 1, 1, "filled")
    assert not registry.reconcile_order_states((first, wrong_second))
    assert registry.unresolved == {"first", "second"}
    assert registry.orders["first"].status == "pending"
