"""Tests for live market/season state tracking and feed ingestion."""

from __future__ import annotations

from ctc.execution.models import Frame, OpenOrder, Position, Side
from ctc.execution.state import LiveState
from ctc.forecasts.estimator import build_live_estimator


def _news(msg: dict) -> Frame:
    return Frame(type="news", raw={"type": "news", "message": msg}, received_at=0.0)


def _full_damage(port=20.0):
    return {"PORT": port, "ISLE": 5.0, "AQUA": 4.0, "GRID": 6.0, "FERRY": 3.0}


def _damage(round_, seq, pct=None, loss=400.0, stage="final"):
    return _news(
        {
            "type": "damage_report",
            "round": round_,
            "stage": stage,
            "seq": seq,
            "damage_pct": pct or _full_damage(),
            "industry_loss_musd": loss,
        }
    )


def _state():
    return LiveState(build_live_estimator())


def test_quote_ingestion_and_views():
    st = _state()
    st.ingest_frame(Frame(type="quote", raw={"type": "quote", "symbol": "PORT", "bid": 70.0, "ask": 70.4}))
    assert st.mids()["PORT"] == 70.2
    assert st.executable()["PORT"] == (70.0, 70.4)


def test_seq_dedup_and_out_of_order():
    st = _state()
    st.ingest_frame(_damage(1, seq=5))
    st.ingest_frame(_damage(1, seq=5))  # duplicate seq
    st.ingest_frame(_damage(2, seq=3))  # out of order (3 < 5)
    assert st.n_duplicates == 1
    assert st.n_out_of_order == 1
    assert st.n_feed_msgs == 2  # the duplicate was dropped before counting


def test_season_rollover_resets_state():
    st = _state()
    st.ingest_frame(_damage(1, seq=1))
    st.ingest_frame(_damage(2, seq=2))
    st.ingest_frame(_damage(3, seq=3))
    assert st.estimator.reported_rounds == {1, 2, 3}
    # a fresh round-1 report after round 3 => new season => reset
    st.ingest_frame(_damage(1, seq=1))
    assert st.current_round == 1
    assert st.estimator.reported_rounds == {1}  # reset then re-ingested


def test_competition_id_change_clears_old_market_state():
    st = _state()
    st.update_exchange_status({"competition_id": "season-a", "state": "running"})
    st.ingest_frame(Frame(type="quote", raw={"type": "quote", "symbol": "PORT", "bid": 70, "ask": 71}))
    st.ingest_frame(Frame(type="orderbook", raw={"type": "orderbook", "symbol": "PORT", "bids": [[70, 2]]}))
    trade = Frame(type="trade", raw={"type": "trade", "symbol": "PORT"})
    st.ingest_frame(trade)
    assert st.market_is_running()

    changed = st.update_exchange_status({"competition_id": "season-b", "state": "ready"})

    assert changed
    assert st.competition_id == "season-b"
    assert st.exchange_state == "ready"
    assert st.quotes == {} and st.books == {}
    assert list(st.recent_trades) == []
    assert not st.market_is_running()


def test_trade_frames_are_retained_without_assuming_payload_shape():
    st = _state()
    frame = Frame(type="trade", raw={"type": "trade", "payload": {"unknown": "shape"}})
    st.ingest_frame(frame)
    assert list(st.recent_trades) == [frame]


def test_stale_symbols_uses_clock():
    now = [1000.0]
    st = LiveState(build_live_estimator(), clock=lambda: now[0])
    raw = {"type": "quote", "symbol": "PORT", "bid": 1.0, "ask": 2.0}
    st.ingest_frame(Frame(type="quote", raw=raw, received_at=1000.0))
    assert st.stale_symbols(max_age_s=5.0) == []
    now[0] = 1010.0
    assert st.stale_symbols(max_age_s=5.0) == ["PORT"]


def test_reconcile_positions_and_orders_and_resting_exposure():
    st = _state()
    st.reconcile_positions([Position("PORT", -8), Position("HAVEN", 5)])
    assert st.positions["PORT"] == -8 and st.positions["HAVEN"] == 5
    st.reconcile_open_orders(
        [
            OpenOrder("o1", "PORT", Side.SELL, quantity=10, price=70.0, filled=4),
            OpenOrder("o2", "PORT", Side.BUY, quantity=3, price=69.0),
        ]
    )
    # resting: -(10-4) + 3 = -3
    assert st.resting_exposure()["PORT"] == -3


def test_snapshot_resync_is_idempotent():
    st = _state()
    st.ingest_frame(_damage(1, seq=1))  # already have round 1
    # signal a reconnect, then deliver a snapshot containing rounds 1 and 2
    st.ingest_frame(Frame(type="_reconnect", raw={}))
    snap = {
        "type": "snapshot",
        "damage": [
            {"round": 1, "stage": "final", "damage_pct": _full_damage(20.0), "industry_loss_musd": 400.0},
            {"round": 2, "stage": "final", "damage_pct": _full_damage(10.0), "industry_loss_musd": 300.0},
        ],
    }
    st.ingest_frame(Frame(type="news", raw={"type": "news", "message": snap}))
    # round 1 not double-counted; round 2 caught up
    assert st.estimator.reported_rounds == {1, 2}
