"""Tests for the live trading orchestrator: dry-run gating, kill switch, churn control."""

from __future__ import annotations

import asyncio

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

    async def place_order(self, req):
        self.placed.append(req)
        return OrderAck(order_id=f"id{len(self.placed)}", symbol=req.symbol, status="accepted")

    async def cancel_all(self, symbol=None):
        self.cancelled += 1
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
    trader, _ = _trader(state, dry_run=False)
    asyncio.run(trader.step())
    assert len(trader.client.placed) >= 1
    assert trader.n_submitted >= 1


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
    trader2, _ = _trader(state, dry_run=False)
    asyncio.run(trader2.shutdown())
    assert trader2.client.cancelled == 1
