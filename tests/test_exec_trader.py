"""Tests for the live trading orchestrator: dry-run gating, kill switch, churn control."""

from __future__ import annotations

import asyncio

from ctc.execution.automation import SessionPnLTracker
from ctc.execution.models import Frame, OrderAck
from ctc.execution.risk import RiskManager
from ctc.execution.settings import LiveConfig
from ctc.execution.state import LiveState
from ctc.execution.trader import LiveTrader
from ctc.forecasts.estimator import build_live_estimator


class FakeClient:
    def __init__(self):
        self.placed = []
        self.cancelled = 0
        self.cancelled_ids = []

    async def place_order(self, req):
        self.placed.append(req)
        return OrderAck(order_id=f"id{len(self.placed)}", symbol=req.symbol, status="accepted")

    async def cancel_order(self, order_id):
        self.cancelled += 1
        self.cancelled_ids.append(order_id)
        return {}

    async def get_positions(self):
        return []

    async def get_open_orders(self):
        return []


def _news(msg):
    return Frame(type="news", raw={"type": "news", "message": msg}, received_at=0.0)


def _overpriced_state():
    """PORT fundamentally wrecked (fair ~0) but quoted ~72 -> strong SELL signal."""
    st = LiveState(build_live_estimator())
    for r in (1, 2, 3):
        st.ingest_frame(
            _news(
                {
                    "type": "damage_report",
                    "round": r,
                    "stage": "final",
                    "seq": 100 + r,
                    "damage_pct": {"PORT": 60.0, "ISLE": 5.0, "AQUA": 4.0, "GRID": 6.0, "FERRY": 3.0},
                    "industry_loss_musd": 400.0,
                }
            )
        )
    st.ingest_frame(Frame(type="quote", raw={"type": "quote", "symbol": "PORT", "bid": 72.0, "ask": 72.2}))
    return st


def _trader(state, dry_run=True):
    cfg = LiveConfig(api_key="x", dry_run=dry_run)
    risk = RiskManager(cfg.max_order_size_frac, cfg.max_gross_exposure)
    return LiveTrader(client=FakeClient(), state=state, risk=risk, cfg=cfg), cfg


def test_propose_generates_sell_on_overpricing():
    trader, _ = _trader(_overpriced_state())
    plog = trader.propose()
    syms = {o.symbol for o in plog.approved}
    assert "PORT" in syms
    assert all(o.side.value == "sell" for o in plog.approved if o.symbol == "PORT")


def test_dry_run_never_submits():
    state = _overpriced_state()
    trader, _ = _trader(state, dry_run=True)
    plog = asyncio.run(trader.step())
    assert len(plog.approved) >= 1
    assert trader.client.placed == []  # nothing sent to the exchange
    assert trader.n_dry_run >= 1


def test_live_mode_submits():
    state = _overpriced_state()
    state.update_exchange_status({"competition_id": "test", "state": "running"})
    trader, _ = _trader(state, dry_run=False)
    trader.automatic_live_authorized = True  # stand-in for all rollout gates passing
    asyncio.run(trader.step())
    assert len(trader.client.placed) >= 1
    assert trader.n_submitted >= 1


def test_live_mode_stays_gated_without_transition_authorization():
    state = _overpriced_state()
    state.update_exchange_status({"competition_id": "test", "state": "running"})
    trader, cfg = _trader(state, dry_run=False)
    cfg.automatic_transition_enabled = True
    asyncio.run(trader.step())
    assert trader.client.placed == []
    assert trader.n_dry_run >= 1


def test_session_loss_latches_and_cancels_only_registered_bot_orders(tmp_path):
    state = _overpriced_state()
    state.update_exchange_status({"competition_id": "test", "state": "running"})
    trader, _ = _trader(state, dry_run=False)
    trader.automatic_live_authorized = True
    asyncio.run(trader.step())
    assert trader.client.placed
    tracker = SessionPnLTracker(tmp_path / "pnl.json", clock=lambda: 10)
    tracker.update(starting_equity=100_000, realized_pnl=0, unrealized_pnl=0, source_verified=True)
    tracker.update(
        starting_equity=100_000,
        realized_pnl=-10_000,
        unrealized_pnl=0,
        source_verified=True,
    )

    assert asyncio.run(trader.enforce_session_loss(tracker))
    assert trader.cfg.kill_switch and trader.risk.kill_switch
    assert trader.client.cancelled_ids == ["id1"]
    # The loss stop cancels bot orders; it does not submit an offsetting liquidation.
    assert len(trader.client.placed) == 1


def test_kill_switch_blocks_submission():
    state = _overpriced_state()
    trader, _ = _trader(state, dry_run=False)
    trader.engage_kill_switch()
    plog = asyncio.run(trader.step())
    assert plog.approved == []  # risk rejects everything under kill switch
    assert trader.client.placed == []


def test_churn_control_skips_unchanged_signal():
    state = _overpriced_state()
    trader, _ = _trader(state, dry_run=True)
    first = trader.propose()
    assert len(first.approved) >= 1
    second = trader.propose()  # same quotes/fair -> no price move
    assert any(reason == "unchanged_signal" for _, reason in second.rejected)
    assert all(o.symbol != "PORT" for o in second.approved)


def test_shutdown_cancels_only_when_live():
    state = _overpriced_state()
    trader, _ = _trader(state, dry_run=True)
    asyncio.run(trader.shutdown())
    assert trader.client.cancelled == 0  # dry-run: no cancels
    state.update_exchange_status({"competition_id": "test", "state": "running"})
    trader2, _ = _trader(state, dry_run=False)
    trader2._submitted_order_ids.add("bot-order")
    asyncio.run(trader2.shutdown())
    assert trader2.client.cancelled == 1
    assert trader2.client.cancelled_ids == ["bot-order"]


def test_shutdown_still_cancels_bot_orders_when_kill_switch_is_engaged():
    state = _overpriced_state()
    state.update_exchange_status({"competition_id": "test", "state": "running"})
    trader, _ = _trader(state, dry_run=False)
    trader._submitted_order_ids.add("bot-order")
    trader.engage_kill_switch()
    asyncio.run(trader.shutdown())
    assert trader.client.cancelled_ids == ["bot-order"]


def test_live_submission_requires_fresh_running_state():
    state = _overpriced_state()
    trader, _ = _trader(state, dry_run=False)
    asyncio.run(trader.step())
    assert trader.client.placed == []
    state.update_exchange_status({"competition_id": "test", "state": "ready"})
    asyncio.run(trader.step())
    assert trader.client.placed == []
