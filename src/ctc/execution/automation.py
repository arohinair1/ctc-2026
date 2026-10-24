"""Fail-closed rollout controls for persistent ownership, session PnL and live gating.

The current exchange protocol does not document an account-equity or fills endpoint.
Consequently these controls accept only explicitly complete, verified accounting input;
they do not infer fills from market tape or limit prices.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host
    fcntl = None

from ctc.config import ALL_SYMBOLS
from ctc.execution.models import OpenOrder, OrderAck, OrderRequest


class TransitionPhase(str, Enum):
    WAITING_FOR_OPEN = "WAITING_FOR_OPEN"
    OBSERVING = "OBSERVING"
    EXECUTION_TEST = "EXECUTION_TEST"
    READY_FOR_LIVE = "READY_FOR_LIVE"
    LIVE = "LIVE"
    SAFE_MODE = "HALTED / SAFE_MODE"


@dataclass
class OwnedOrder:
    """Durable local evidence that an exchange order ID came from this bot."""

    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float | None
    client_tag: str | None
    status: str = "submitted"
    filled_quantity: float = 0.0
    execution_ids: list[str] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    cancellation_confirmed: bool = False


class OrderRegistryError(RuntimeError):
    """Registry is incomplete or conflicts with exchange state."""


@dataclass(frozen=True)
class VerifiedExecution:
    """An authoritative fill record tied to an exchange order ID."""

    order_id: str
    execution_id: str
    quantity: float
    price: float


@dataclass(frozen=True)
class VerifiedOrderState:
    order_id: str
    symbol: str
    side: str
    quantity: int
    filled: float
    status: str


@dataclass(frozen=True)
class VerifiedPnLSnapshot:
    """Complete account snapshot supplied by an independently verified provider."""

    starting_equity: float | None
    realized_pnl: float | None
    unrealized_pnl: float | None
    as_of: float
    source_verified: bool
    execution_history_complete: bool
    executions: tuple[VerifiedExecution, ...] = ()
    account_pnl_complete: bool = False
    order_states: tuple[VerifiedOrderState, ...] = ()


class ProcessLease:
    """Non-blocking singleton lock for cooperating bot processes on POSIX hosts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: Any = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> bool:
        if fcntl is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._file = handle
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if self._file is None:
            return
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


class BotOrderRegistry:
    """Atomic persistent registry; only IDs recorded after our own POST are ours.

    Exchange `/orders/open` contains no documented owner/team field. Unrecognized remote
    orders, unresolved submissions, or locally-active orders missing remotely therefore
    make ownership reconciliation incomplete. Callers must never cancel those orders.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.orders: dict[str, OwnedOrder] = {}
        self.pending_intents: dict[str, dict[str, Any]] = {}
        self.unresolved: set[str] = set()
        self.last_reconciliation_complete = False
        self.execution_history_complete = False
        self.order_history_complete = False
        self.manual_orders: dict[str, dict[str, Any]] = {}
        self.execution_test_attempted = False
        self.execution_test_result: dict[str, Any] | None = None
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            data = json.loads(self.path.read_text())
            self.orders = {k: OwnedOrder(**v) for k, v in data.get("orders", {}).items()}
            self.manual_orders = data.get("manual_orders", {})
            self.pending_intents = data.get("pending_intents", {})
            self.unresolved = set(data.get("unresolved", []))
            self.execution_history_complete = False  # refreshed from the provider on every start
            self.execution_test_attempted = bool(data.get("execution_test_attempted", False))
            self.execution_test_result = data.get("execution_test_result")
            self.last_reconciliation_complete = False  # always reconcile after restart
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise OrderRegistryError("persistent order registry is unreadable") from exc

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "orders": {k: asdict(v) for k, v in self.orders.items()},
            "manual_orders": self.manual_orders,
            "pending_intents": self.pending_intents,
            "unresolved": sorted(self.unresolved),
            "execution_history_complete": self.execution_history_complete,
            "execution_test_attempted": self.execution_test_attempted,
            "execution_test_result": self.execution_test_result,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".order-registry-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def begin_submission(self, request: OrderRequest) -> str:
        intent_id = str(uuid.uuid4())
        self.pending_intents[intent_id] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "quantity": request.quantity,
            "price": request.price,
            "client_tag": request.client_tag,
        }
        self.last_reconciliation_complete = False
        self._save()
        return intent_id

    def record_ack(self, intent_id: str, ack: OrderAck) -> None:
        if intent_id not in self.pending_intents:
            raise OrderRegistryError("acknowledgement has no persisted submission intent")
        intent = self.pending_intents.pop(intent_id)
        if not ack.order_id:
            self.unresolved.add(intent_id)
            self._save()
            return
        if ack.order_id in self.orders:
            self.unresolved.add(ack.order_id)
            self._save()
            raise OrderRegistryError("exchange reused an order ID already in the bot registry")
        self.orders[ack.order_id] = OwnedOrder(
            order_id=ack.order_id,
            symbol=ack.symbol or intent["symbol"],
            side=intent["side"],
            quantity=int(intent["quantity"]),
            price=intent["price"],
            client_tag=intent["client_tag"],
            status=(ack.status or "unknown").lower(),
            filled_quantity=float(ack.filled),
        )
        if ack.status is None:
            self.unresolved.add(ack.order_id)
        self._save()

    def record_failure(self, intent_id: str, *, definite_rejection: bool) -> None:
        if intent_id not in self.pending_intents:
            raise OrderRegistryError("failed submission has no persisted intent")
        self.pending_intents.pop(intent_id)
        if not definite_rejection:
            self.unresolved.add(intent_id)
        self._save()

    def record_execution(self, order_id: str, execution_id: str, quantity: float, price: float) -> None:
        order = self.orders.get(order_id)
        if order is None:
            raise OrderRegistryError("execution references an order not owned by this bot")
        if not execution_id or not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("execution id and positive finite quantity are required")
        if not math.isfinite(price) or price <= 0:
            raise ValueError("execution price must be positive and finite")
        if execution_id in order.execution_ids:
            return
        recorded_qty = sum(float(fill["quantity"]) for fill in order.executions)
        if recorded_qty + quantity > order.quantity + 1e-9:
            raise OrderRegistryError("execution quantity exceeds submitted quantity")
        order.execution_ids.append(execution_id)
        order.executions.append(
            {"execution_id": execution_id, "quantity": float(quantity), "price": float(price)}
        )
        order.filled_quantity = max(order.filled_quantity, recorded_qty + quantity)
        order.status = "filled" if order.filled_quantity >= order.quantity else "partially_filled"
        self._save()

    def reconcile_executions(
        self, executions: Iterable[VerifiedExecution], *, history_complete: bool
    ) -> bool:
        """Apply authoritative fills; an unknown order ID makes reconciliation fail."""
        self.execution_history_complete = False
        if not history_complete:
            self._save()
            return False
        try:
            for execution in executions:
                self.record_execution(
                    execution.order_id,
                    execution.execution_id,
                    execution.quantity,
                    execution.price,
                )
        except (OrderRegistryError, TypeError, ValueError):
            self.last_reconciliation_complete = False
            self._save()
            return False
        self.execution_history_complete = True
        self._save()
        return self.execution_records_complete

    def begin_execution_test(self) -> None:
        """Persist the one-shot PORT test attempt before any order submission."""
        if self.execution_test_attempted:
            raise OrderRegistryError("PORT execution test was already attempted")
        self.execution_test_attempted = True
        self._save()

    def record_execution_test_result(self, evidence: dict[str, Any]) -> None:
        if not self.execution_test_attempted:
            raise OrderRegistryError("PORT test result has no persisted attempt")
        self.execution_test_result = dict(evidence)
        self._save()

    def reset_execution_test(self) -> None:
        """Allow the one-shot test to run in a new competition season only."""
        self.execution_test_attempted = False
        self.execution_test_result = None
        self._save()

    def record_cancel(self, order_id: str, *, confirmed: bool) -> None:
        order = self.orders.get(order_id)
        if order is None:
            raise OrderRegistryError("refusing to record/cancel an unowned order")
        if confirmed:
            order.cancellation_confirmed = True
            if order.filled_quantity < order.quantity:
                order.status = "cancelled"
        else:
            self.unresolved.add(order_id)
        self._save()

    def preserve_manual_orders(self, orders: list[OpenOrder], *, manual_authorization: bool) -> None:
        """Recognize explicitly identified manual IDs without owning or cancelling them."""
        if not manual_authorization:
            raise PermissionError("manual-order identification requires operator authorization")
        for order in orders:
            if not order.order_id or order.order_id in self.orders:
                raise OrderRegistryError("manual order overlaps bot-owned registry")
            self.manual_orders[order.order_id] = {
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "price": order.price,
            }
            self.unresolved.discard(order.order_id)
        self._save()

    def reconcile_order_states(self, states: tuple[VerifiedOrderState, ...]) -> bool:
        """Use account order history for quantities/status, never invent execution prices.

        This path is used only with independently authoritative cumulative account PnL.
        A truncated history that omits any of our IDs fails closed.
        """
        self.order_history_complete = False
        remote = {state.order_id: state for state in states}
        if len(remote) != len(states) or self.pending_intents:
            return False
        for oid, local in self.orders.items():
            state = remote.get(oid)
            if state is None:
                return False
            if (
                state.symbol != local.symbol
                or state.side != local.side
                or state.quantity != local.quantity
                or not math.isfinite(state.filled)
                or not local.filled_quantity <= state.filled <= local.quantity
            ):
                return False
            status = state.status.lower().replace("canceled", "cancelled")
            if status not in {
                "open",
                "pending",
                "new",
                "accepted",
                "partially_filled",
                "filled",
                "cancelled",
                "rejected",
            }:
                return False
            if status == "filled" and state.filled != local.quantity:
                return False
            local.filled_quantity = state.filled
            local.status = status
            if status == "cancelled":
                local.cancellation_confirmed = True
        self.order_history_complete = True
        self._save()
        return not self.pending_intents and not self.unresolved

    def reconcile(self, exchange_orders: list[OpenOrder]) -> bool:
        for order in exchange_orders:
            manual = self.manual_orders.get(order.order_id)
            if manual is not None and (
                order.symbol != manual["symbol"]
                or order.side.value != manual["side"]
                or order.quantity != manual["quantity"]
                or order.price != manual["price"]
            ):
                self.last_reconciliation_complete = False
                return False
        remote = {
            o.order_id: o for o in exchange_orders if o.order_id and o.order_id not in self.manual_orders
        }
        unknown = set(remote) - set(self.orders)
        remote_terminal = {
            oid
            for oid in set(remote) & set(self.orders)
            if self.orders[oid].status in {"filled", "cancelled", "rejected"}
            or self.orders[oid].cancellation_confirmed
        }
        active_local = {
            oid
            for oid, order in self.orders.items()
            if order.status not in {"filled", "cancelled", "rejected"} and not order.cancellation_confirmed
        }
        missing_remote = active_local - set(remote)
        self.unresolved.update(unknown | missing_remote)
        if unknown or missing_remote or remote_terminal or self.pending_intents:
            self.unresolved.update(remote_terminal)
            self.last_reconciliation_complete = False
            self._save()
            return False
        for order_id in active_local:
            remote_order = remote[order_id]
            local = self.orders[order_id]
            if (
                remote_order.symbol != local.symbol
                or remote_order.side.value != local.side
                or int(remote_order.quantity) != local.quantity
            ):
                self.unresolved.add(order_id)
                self.last_reconciliation_complete = False
                self._save()
                return False
            # Filled amount from /orders/open is useful for reconciliation but has no
            # execution price; do not treat it as a PnL execution record.
            if remote_order.filled > local.filled_quantity:
                local.filled_quantity = float(remote_order.filled)
                local.status = "partially_filled"
        self.last_reconciliation_complete = not self.unresolved
        self._save()
        return self.last_reconciliation_complete

    @property
    def ownership_verified(self) -> bool:
        return self.last_reconciliation_complete and not self.pending_intents and not self.unresolved

    @property
    def execution_records_complete(self) -> bool:
        if self.order_history_complete:
            return not self.pending_intents and not self.unresolved
        if not self.execution_history_complete:
            return False
        for order in self.orders.values():
            recorded = sum(float(fill["quantity"]) for fill in order.executions)
            if order.status == "filled":
                if recorded + 1e-9 < order.quantity:
                    return False
            elif not math.isclose(recorded, order.filled_quantity, rel_tol=0, abs_tol=1e-9):
                return False
        return not self.pending_intents

    def known_open_ids(self) -> tuple[str, ...]:
        if not self.ownership_verified:
            raise OrderRegistryError("order ownership is not reconciled; refusing cancellation")
        return tuple(
            oid
            for oid, order in self.orders.items()
            if order.status not in {"filled", "cancelled", "rejected"} and not order.cancellation_confirmed
        )


@dataclass
class SessionPnLTracker:
    """Session equity/PnL ledger; incomplete or stale inputs are never treated as zero."""

    path: Path | None = None
    loss_limit: float = 10_000.0
    max_age_s: float = 10.0
    clock: Callable[[], float] = time.time
    starting_equity: float | None = None
    source_starting_equity: float | None = None
    baseline_realized: float | None = None
    baseline_unrealized: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    verified_at: float | None = None
    source_verified: bool = False
    loss_latched: bool = False
    pnl_only: bool = False

    def __post_init__(self) -> None:
        if self.loss_limit <= 0 or self.max_age_s <= 0:
            raise ValueError("PnL loss limit and freshness must be positive")
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                for name in (
                    "starting_equity",
                    "source_starting_equity",
                    "baseline_realized",
                    "baseline_unrealized",
                    "realized_pnl",
                    "unrealized_pnl",
                    "verified_at",
                    "source_verified",
                    "loss_latched",
                    "pnl_only",
                ):
                    setattr(self, name, data.get(name))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("persistent PnL ledger is unreadable") from exc
            # Inputs must be refreshed against the live account after each restart.
            self.source_verified = False

    def update(
        self,
        *,
        starting_equity: float | None,
        realized_pnl: float | None,
        unrealized_pnl: float | None,
        source_verified: bool,
        account_pnl_complete: bool = False,
    ) -> bool:
        pnl_only = account_pnl_complete and starting_equity is None
        values = (
            (realized_pnl, unrealized_pnl) if pnl_only else (starting_equity, realized_pnl, unrealized_pnl)
        )
        complete = (
            source_verified
            and all(v is not None and math.isfinite(float(v)) for v in values)
            and (pnl_only or float(starting_equity) > 0)
        )
        if not complete:
            self.source_verified = False
            self.verified_at = None
            self._save()
            return False
        if self.baseline_realized is None:
            self.pnl_only = pnl_only
            self.source_starting_equity = None if pnl_only else float(starting_equity)
            self.starting_equity = self.source_starting_equity
            self.baseline_realized = float(realized_pnl)
            self.baseline_unrealized = float(unrealized_pnl)
        elif self.pnl_only != pnl_only or (
            not pnl_only
            and not math.isclose(float(starting_equity), self.source_starting_equity, rel_tol=0, abs_tol=0.01)
        ):
            self.source_verified = False
            self.verified_at = None
            self._save()
            return False
        self.realized_pnl = float(realized_pnl)
        self.unrealized_pnl = float(unrealized_pnl)
        self.source_verified = True
        self.verified_at = self.clock()
        self.loss_latched = self.loss_latched or self.loss_limit_reached
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            key: getattr(self, key)
            for key in (
                "starting_equity",
                "source_starting_equity",
                "baseline_realized",
                "baseline_unrealized",
                "realized_pnl",
                "unrealized_pnl",
                "verified_at",
                "source_verified",
                "loss_latched",
                "pnl_only",
            )
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".session-pnl-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @property
    def complete(self) -> bool:
        return (
            self.source_verified
            and self.verified_at is not None
            and self.clock() - self.verified_at <= self.max_age_s
            and (self.pnl_only or self.starting_equity is not None)
            and self.baseline_realized is not None
            and self.baseline_unrealized is not None
            and self.realized_pnl is not None
            and self.unrealized_pnl is not None
        )

    @property
    def session_pnl(self) -> float | None:
        if not self.complete:
            return None
        assert self.baseline_realized is not None and self.baseline_unrealized is not None
        assert self.realized_pnl is not None and self.unrealized_pnl is not None
        return (self.realized_pnl + self.unrealized_pnl) - (self.baseline_realized + self.baseline_unrealized)

    @property
    def loss_limit_reached(self) -> bool:
        pnl = self.session_pnl
        return self.loss_latched or (pnl is not None and pnl <= -self.loss_limit)

    def authorize_resume(self, *, manual_authorization: bool) -> None:
        if not manual_authorization:
            raise PermissionError("manual authorization is required after a loss-limit stop")
        if not self.complete:
            raise ValueError("cannot resume without a fresh, complete PnL snapshot")
        assert self.realized_pnl is not None and self.unrealized_pnl is not None
        assert self.session_pnl is not None
        if self.starting_equity is not None:
            self.starting_equity += self.session_pnl
        self.baseline_realized = self.realized_pnl
        self.baseline_unrealized = self.unrealized_pnl
        self.loss_latched = False
        self._save()


@dataclass(frozen=True)
class SafetySnapshot:
    exchange_running: bool
    websocket_healthy: bool
    fresh_symbols: frozenset[str]
    execution_api_healthy: bool
    positive_net_edge: bool
    limits_enforced: bool
    pnl_complete: bool
    loss_limit_clear: bool
    ownership_verified: bool
    executions_complete: bool
    process_healthy: bool
    no_unexplained_discrepancy: bool


@dataclass(frozen=True)
class ExecutionTestEvidence:
    """Read-back evidence for the single approved one-share PORT test."""

    order_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: float
    best_bid_before: float
    fill_quantity: float
    appeared_in_open_orders: bool
    cancellation_acknowledged: bool
    absent_after_cancel: bool
    port_position_before: int
    port_position_after: int
    competition_id: str | None = None
    execution_integrity: bool = True
    best_ask_before: float | None = None

    @property
    def passed(self) -> bool:
        return all(
            (
                bool(self.order_id),
                self.symbol == "PORT",
                self.side in {"buy", "sell"},
                self.quantity == 1,
                math.isfinite(self.limit_price) and self.limit_price > 0,
                math.isfinite(self.best_bid_before) and self.best_bid_before > 0,
                (self.side == "buy" and self.limit_price < self.best_bid_before)
                or (
                    self.side == "sell"
                    and self.best_ask_before is not None
                    and math.isfinite(self.best_ask_before)
                    and self.best_ask_before > 0
                    and self.limit_price > self.best_ask_before
                ),
                math.isfinite(self.fill_quantity),
                self.fill_quantity == 0,
                self.appeared_in_open_orders,
                self.cancellation_acknowledged,
                self.absent_after_cancel,
                self.port_position_before == self.port_position_after,
                self.execution_integrity,
            )
        )


@dataclass
class AutomaticTransition:
    """In-process transition controller. It never executes the PORT test itself."""

    live_activation_enabled: bool = False
    observe_seconds: float = 30.0
    evaluation_interval_s: float = 5.0
    required_symbols: frozenset[str] = frozenset(ALL_SYMBOLS)
    clock: Callable[[], float] = time.monotonic
    phase: TransitionPhase = TransitionPhase.WAITING_FOR_OPEN
    observation_started_at: float | None = None
    last_observation_at: float | None = None
    observation_samples: int = 0
    positive_edge_samples: int = 0
    execution_test_passed: bool = False
    reason: str = "waiting for active exchange and fresh quotes"
    _last_snapshot: SafetySnapshot | None = None

    def record_execution_test(self, evidence: ExecutionTestEvidence) -> None:
        if self.phase is not TransitionPhase.EXECUTION_TEST:
            raise RuntimeError("execution test result is only accepted in EXECUTION_TEST")
        if not evidence.passed:
            self.phase = TransitionPhase.SAFE_MODE
            self.reason = "execution test evidence failed validation"
            return
        self.execution_test_passed = True
        self.reason = f"execution test passed for exchange order {evidence.order_id}"

    def fail_execution_test(self, reason: str) -> None:
        if self.phase is not TransitionPhase.EXECUTION_TEST:
            raise RuntimeError("execution test result is only accepted in EXECUTION_TEST")
        self.phase = TransitionPhase.SAFE_MODE
        self.reason = reason

    def tick(self, snapshot: SafetySnapshot, *, observe: bool = False) -> TransitionPhase:
        self._last_snapshot = snapshot
        if not snapshot.exchange_running:
            if self.phase in {TransitionPhase.WAITING_FOR_OPEN, TransitionPhase.OBSERVING}:
                self.phase = TransitionPhase.WAITING_FOR_OPEN
                self.observation_started_at = None
                self.last_observation_at = None
                self.observation_samples = 0
                self.positive_edge_samples = 0
                self.reason = "exchange not running; monitoring continues"
            elif self.phase in {
                TransitionPhase.EXECUTION_TEST,
                TransitionPhase.READY_FOR_LIVE,
                TransitionPhase.LIVE,
            }:
                self.phase = TransitionPhase.WAITING_FOR_OPEN
                self.observation_started_at = None
                self.last_observation_at = None
                self.observation_samples = 0
                self.positive_edge_samples = 0
                self.reason = "exchange halted; monitoring and waiting for a fresh observation window"
            return self.phase

        if (
            not snapshot.process_healthy
            or not snapshot.no_unexplained_discrepancy
            or (snapshot.pnl_complete and not snapshot.loss_limit_clear)
        ):
            self.phase = TransitionPhase.SAFE_MODE
            self.reason = "process health or reconciliation discrepancy failed"
            return self.phase

        if self.phase is TransitionPhase.WAITING_FOR_OPEN:
            if self._observation_inputs_ready(snapshot):
                self.phase = TransitionPhase.OBSERVING
                self.observation_started_at = self.clock()
                self.last_observation_at = None
                self.observation_samples = 0
                self.positive_edge_samples = 0
                self.reason = "collecting the 30-second live observation"
            return self.phase

        if self.phase is TransitionPhase.OBSERVING:
            if not self._observation_inputs_ready(snapshot):
                self.observation_started_at = None
                self.observation_samples = 0
                self.positive_edge_samples = 0
                self.reason = "observation reset: feed or quotes are not healthy"
                return self.phase
            now = self.clock()
            if self.observation_started_at is None:
                self.observation_started_at = now
                self.last_observation_at = None
                self.observation_samples = 0
                self.positive_edge_samples = 0
            if observe and (
                self.last_observation_at is None
                or now - self.last_observation_at >= self.evaluation_interval_s
            ):
                self.observation_samples += 1
                self.positive_edge_samples += int(snapshot.positive_net_edge)
                self.last_observation_at = now
            elapsed = now - self.observation_started_at
            enough_edge = self.positive_edge_samples >= math.ceil(self.observation_samples / 2)
            if elapsed >= self.observe_seconds and self.observation_samples >= 6 and enough_edge:
                self.phase = TransitionPhase.EXECUTION_TEST
                self.reason = "observation complete with positive net edge; approved execution test required"
            return self.phase

        if self.phase is TransitionPhase.EXECUTION_TEST:
            if self.execution_test_passed and self._non_alpha_gates_pass(snapshot):
                self.phase = TransitionPhase.READY_FOR_LIVE
                self.reason = "all verification gates passed; live activation remains separately disabled"
            else:
                self.reason = "waiting for verified execution test and remaining safety gates"
            return self.phase

        if self.phase is TransitionPhase.READY_FOR_LIVE:
            if self._fatal_gates_fail(snapshot):
                self.phase = TransitionPhase.SAFE_MODE
                self.reason = "a required live safety gate failed"
            elif not self._non_alpha_gates_pass(snapshot):
                # A temporarily stale feed, unavailable endpoint, missing accounting refresh
                # or no current edge simply keeps the bot in dry-run and monitoring.
                self.reason = "live gate unavailable; continuing to monitor in dry-run"
            elif not self._alpha_gate_passes(snapshot):
                self.reason = "no current positive net edge; continuing to monitor in dry-run"
            elif self.live_activation_enabled:
                self.phase = TransitionPhase.LIVE
                self.reason = "all gates passed and automatic activation was explicitly enabled"
            return self.phase

        if self.phase is TransitionPhase.LIVE:
            if self._fatal_gates_fail(snapshot):
                self.phase = TransitionPhase.SAFE_MODE
                self.reason = "loss limit or integrity fault; new orders halted"
            elif not self._operational_gates_pass(snapshot):
                self.phase = TransitionPhase.OBSERVING
                self.observation_started_at = None
                self.last_observation_at = None
                self.observation_samples = 0
                self.positive_edge_samples = 0
                self.reason = "feed or quote health failed; dry-run until a new observation window passes"
        return self.phase

    def authorize_resume(self, *, manual_authorization: bool) -> None:
        if not manual_authorization:
            raise PermissionError("manual authorization is required to leave SAFE_MODE")
        self.phase = TransitionPhase.WAITING_FOR_OPEN
        self.execution_test_passed = False
        self.observation_started_at = None
        self.last_observation_at = None
        self.observation_samples = 0
        self.positive_edge_samples = 0
        self.reason = "manually resumed into WAITING_FOR_OPEN; all gates must be rechecked"

    def _observation_inputs_ready(self, s: SafetySnapshot) -> bool:
        return (
            s.exchange_running
            and s.websocket_healthy
            and self.required_symbols.issubset(s.fresh_symbols)
            and s.process_healthy
            and s.no_unexplained_discrepancy
        )

    def _operational_gates_pass(self, s: SafetySnapshot) -> bool:
        return all(
            (
                s.exchange_running,
                s.websocket_healthy,
                self.required_symbols.issubset(s.fresh_symbols),
                s.process_healthy,
                s.no_unexplained_discrepancy,
            )
        )

    def _non_alpha_gates_pass(self, s: SafetySnapshot) -> bool:
        return all(
            (
                self._operational_gates_pass(s),
                s.execution_api_healthy,
                s.limits_enforced,
                s.pnl_complete,
                s.loss_limit_clear,
                s.ownership_verified,
                s.executions_complete,
            )
        )

    def _alpha_gate_passes(self, s: SafetySnapshot) -> bool:
        return (
            s.positive_net_edge
            and self.observation_samples >= 6
            and self.positive_edge_samples >= math.ceil(self.observation_samples / 2)
        )

    @staticmethod
    def _fatal_gates_fail(s: SafetySnapshot) -> bool:
        return (
            not s.process_healthy
            or not s.no_unexplained_discrepancy
            or (s.pnl_complete and not s.loss_limit_clear)
        )
