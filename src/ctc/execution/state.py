"""Live market- and season-state tracking.

:class:`LiveState` consumes parsed :class:`~ctc.execution.models.Frame` objects and keeps:

* top-of-book quotes (and optional depth) per symbol, with freshness timestamps;
* the leakage-safe :class:`~ctc.forecasts.estimator.SeasonEstimator`, fed by news/feed
  messages in ``seq`` order with duplicate suppression;
* round / storm / phase and *season* transitions (positions reset to zero and ``seq``
  restarts each new season on the testnet);
* reconciled positions and open orders from REST.

Fundamental valuation and uncertainty come straight from the existing estimator — no live
code re-derives the models. After a reconnect, a ``snapshot`` frame is used to resync
without double-counting already-ingested damage reports.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ctc.config import ALL_SYMBOLS
from ctc.execution.log import get_logger
from ctc.execution.models import Frame, OpenOrder, OrderBook, Position, Quote
from ctc.forecasts.estimator import SeasonEstimator

log = get_logger("state")

# feed message types the estimator consumes
_ESTIMATOR_TYPES = {"storm", "damage_report"}
_TRANSITION_TYPES = {"storm_formed", "impact", "advisory", "damage_report", "storm", "snapshot"}


@dataclass
class LiveState:
    estimator: SeasonEstimator
    clock: Any = time.time  # injectable for tests

    quotes: dict[str, Quote] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    positions: dict[str, int] = field(default_factory=dict)
    open_orders: dict[str, OpenOrder] = field(default_factory=dict)

    competition_id: str | None = None
    current_round: int | None = None
    current_storm: str | None = None
    phase: str | None = None

    _seen_seq: set[int] = field(default_factory=set)
    _resync_pending: bool = False
    n_frames: int = 0
    n_feed_msgs: int = 0
    n_duplicates: int = 0
    n_out_of_order: int = 0
    _max_seq: int = -1

    def __post_init__(self) -> None:
        self.positions = dict.fromkeys(ALL_SYMBOLS, 0)

    # ---- frame ingestion ----------------------------------------------
    def ingest_frame(self, frame: Frame) -> None:
        self.n_frames += 1
        t = frame.type
        if t == "_reconnect":
            self._resync_pending = True
            log.info("reconnect signalled; awaiting snapshot to resync")
            return
        if t == "quote":
            q = Quote.parse(frame.raw, received_at=frame.received_at or self.clock())
            if q.symbol:
                self.quotes[q.symbol] = q
            return
        if t == "orderbook":
            b = OrderBook.parse(frame.raw, received_at=frame.received_at or self.clock())
            if b.symbol:
                self.books[b.symbol] = b
            return
        if t == "news":
            msg = frame.message
            if msg is not None:
                self._handle_feed_message(msg)
            return
        if t in _ESTIMATOR_TYPES or t == "snapshot":
            # some deployments may deliver feed messages unwrapped
            self._handle_feed_message(frame.raw)

    # ---- feed / estimator ---------------------------------------------
    def _handle_feed_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        self._check_season_transition(msg)

        if mtype == "snapshot":
            if self._resync_pending:
                self._resync_from_snapshot(msg)
                self._resync_pending = False
            return

        seq = msg.get("seq")
        if isinstance(seq, int):
            if seq in self._seen_seq:
                self.n_duplicates += 1
                return
            if seq < self._max_seq:
                self.n_out_of_order += 1
            self._seen_seq.add(seq)
            self._max_seq = max(self._max_seq, seq)

        self.n_feed_msgs += 1
        self._track_round_storm(msg)
        if mtype in _ESTIMATOR_TYPES:
            self.estimator.ingest(msg)

    def _track_round_storm(self, msg: dict[str, Any]) -> None:
        r = msg.get("round")
        if isinstance(r, int):
            self.current_round = r
        storm = msg.get("storm")
        if storm and storm != self.current_storm:
            self.current_storm = storm
            log.info("round %s storm %s", self.current_round, storm)

    def _check_season_transition(self, msg: dict[str, Any]) -> None:
        """Detect a new season from a round-1 restart after a later round was seen."""
        r = msg.get("round")
        if isinstance(r, int) and r == 1 and self.current_round in (2, 3):
            log.info("season rollover detected (round reset to 1); resetting state")
            self.on_new_season()

    def on_new_season(self, competition_id: str | None = None) -> None:
        self.estimator.reset()
        self._seen_seq.clear()
        self._max_seq = -1
        self.current_round = None
        self.current_storm = None
        self.positions = dict.fromkeys(ALL_SYMBOLS, 0)
        self.open_orders.clear()
        if competition_id is not None:
            self.competition_id = competition_id

    def _resync_from_snapshot(self, snap: dict[str, Any]) -> None:
        """Idempotently catch up damage reports missed during a disconnect.

        A snapshot carries ``damage`` (all reports so far, oldest first). We only ingest a
        report whose round the estimator has not already recorded, so this never
        double-counts. Outage reconstruction may under-count observations missed while
        disconnected (documented in docs/EXCHANGE.md)."""
        for report in snap.get("damage", []) or []:
            rr = report.get("round")
            stage = report.get("stage")
            already = rr in self.estimator.reported_rounds
            if stage == "final" and not already:
                self.estimator.ingest({**report, "type": "damage_report"})
            elif stage == "preliminary" and not already and rr not in self.estimator.prelim:
                self.estimator.ingest({**report, "type": "damage_report"})
        log.info("resynced from snapshot: reported_rounds=%s", sorted(self.estimator.reported_rounds))

    # ---- reconciliation -----------------------------------------------
    def reconcile_positions(self, positions: list[Position]) -> None:
        new = dict.fromkeys(ALL_SYMBOLS, 0)
        for p in positions:
            if p.symbol in new:
                new[p.symbol] = int(p.quantity)
        self.positions = new

    def reconcile_open_orders(self, orders: list[OpenOrder]) -> None:
        self.open_orders = {o.order_id: o for o in orders if o.order_id}

    # ---- views for the trader -----------------------------------------
    def mids(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym, q in self.quotes.items():
            m = q.mid
            if m is not None:
                out[sym] = m
        return out

    def executable(self) -> dict[str, tuple[float | None, float | None]]:
        """Per-symbol (bid, ask) from live quotes — the prices we can actually transact at."""
        return {sym: (q.bid, q.ask) for sym, q in self.quotes.items()}

    def fair_values_with_uncertainty(self) -> tuple[dict[str, float], dict[str, float]]:
        return self.estimator.fair_values_with_uncertainty()

    def resting_exposure(self) -> dict[str, int]:
        """Signed resting-order quantity per symbol (counts toward position limits)."""
        exp = dict.fromkeys(ALL_SYMBOLS, 0)
        for o in self.open_orders.values():
            if o.symbol in exp:
                exp[o.symbol] += int(round(o.signed_remaining))
        return exp

    def stale_symbols(self, max_age_s: float, now: float | None = None) -> list[str]:
        now = now if now is not None else self.clock()
        return [sym for sym, q in self.quotes.items() if now - q.received_at > max_age_s]

    def summary(self) -> dict[str, Any]:
        return {
            "round": self.current_round,
            "storm": self.current_storm,
            "frames": self.n_frames,
            "feed_msgs": self.n_feed_msgs,
            "duplicates": self.n_duplicates,
            "out_of_order": self.n_out_of_order,
            "quotes": len(self.quotes),
            "reported_rounds": sorted(self.estimator.reported_rounds),
        }
