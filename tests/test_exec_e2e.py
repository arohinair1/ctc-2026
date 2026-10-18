"""Deterministic end-to-end simulation of the live pipeline (no network, no raw data).

Synthesizes a full 3-round season of feed messages plus quotes, drives it through the exact
live interfaces (LiveState.ingest_frame -> LiveTrader.step) in dry-run, and asserts the
pipeline reacts to fundamentals, respects risk limits, is reproducible, and never submits.
"""

from __future__ import annotations

import asyncio

from ctc.config import START_PRICES
from ctc.execution.models import Frame, OrderAck
from ctc.execution.risk import RiskManager
from ctc.execution.settings import LiveConfig
from ctc.execution.state import LiveState
from ctc.execution.trader import LiveTrader
from ctc.forecasts.estimator import build_live_estimator
from ctc.risk.limits import position_limit


class _RecordingClient:
    def __init__(self):
        self.placed = []

    async def place_order(self, req):
        self.placed.append(req)
        return OrderAck(order_id=f"id{len(self.placed)}", symbol=req.symbol, status="accepted")

    async def get_positions(self):
        return []

    async def get_open_orders(self):
        return []

    async def cancel_all(self, symbol=None):
        return {}


def _news(msg):
    return Frame(type="news", raw={"type": "news", "message": msg}, received_at=0.0)


def _season_frames():
    """A deterministic season: heavy, escalating damage with stale (START) quotes."""
    frames = []
    seq = 0
    damage_by_round = [
        {"PORT": 30.0, "ISLE": 10.0, "AQUA": 8.0, "GRID": 20.0, "FERRY": 6.0},
        {"PORT": 25.0, "ISLE": 8.0, "AQUA": 6.0, "GRID": 15.0, "FERRY": 5.0},
        {"PORT": 20.0, "ISLE": 6.0, "AQUA": 5.0, "GRID": 10.0, "FERRY": 4.0},
    ]
    for r, dmg in enumerate(damage_by_round, start=1):
        seq += 1
        frames.append(
            _news(
                {
                    "type": "damage_report",
                    "round": r,
                    "stage": "final",
                    "seq": seq,
                    "damage_pct": dmg,
                    "industry_loss_musd": 400.0,
                }
            )
        )
        # quotes keep printing the (now-stale) START prices -> mispricing to trade
        for sym, px in START_PRICES.items():
            frames.append(
                Frame(
                    type="quote",
                    raw={"type": "quote", "symbol": sym, "bid": px - 0.05, "ask": px + 0.05},
                    received_at=0.0,
                )
            )
    return frames


def _run_season(dry_run=True):
    cfg = LiveConfig(api_key="x", dry_run=dry_run)
    state = LiveState(build_live_estimator())
    risk = RiskManager(cfg.max_order_size_frac, cfg.max_gross_exposure)
    trader = LiveTrader(client=_RecordingClient(), state=state, risk=risk, cfg=cfg)
    all_approved = []
    for frame in _season_frames():
        state.ingest_frame(frame)
        if frame.type == "quote":
            plog = asyncio.run(trader.step())
            all_approved.extend(plog.approved)
    return trader, state, all_approved


def test_e2e_reacts_to_fundamentals_and_respects_limits():
    trader, state, approved = _run_season(dry_run=True)
    # season fully ingested
    assert state.estimator.reported_rounds == {1, 2, 3}
    # pipeline generated trades
    assert len(approved) > 0
    # every approved order respects per-order size and position limits
    for o in approved:
        assert o.quantity >= 1
        assert o.quantity <= trader.risk.max_order_size(o.symbol)
        assert o.quantity <= position_limit(o.symbol)
    # damaged island businesses are sold (fair << quoted START price)
    assert any(o.symbol == "PORT" and o.side.value == "sell" for o in approved)


def test_e2e_dry_run_submits_nothing():
    trader, _, approved = _run_season(dry_run=True)
    assert len(approved) > 0  # decisions were made
    assert trader.client.placed == []  # but nothing was sent
    assert trader.n_submitted == 0


def test_e2e_is_deterministic():
    _, _, a1 = _run_season(dry_run=True)
    _, _, a2 = _run_season(dry_run=True)

    def key(orders):
        return [(o.symbol, o.side.value, o.quantity, o.price) for o in orders]

    assert key(a1) == key(a2)
