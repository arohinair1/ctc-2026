"""Live trading pipeline: state → forecast/valuation → strategy → risk → execution.

:class:`LiveTrader` is the orchestrator. The decision stage (:meth:`propose`) is pure and
testable (no I/O): it reads live quotes and the estimator's fair values, runs the existing
strategy against *real* bid/ask, and passes every candidate through the centralized
:class:`~ctc.execution.risk.RiskManager`. The execution stage (:meth:`execute`) is the only
place that talks to the exchange and it is hard-gated by ``dry_run`` / ``kill_switch``.

Churn control: an order is only (re)proposed for a symbol when there is no equivalent
resting order and the target moved beyond ``min_reprice_edge`` since the last proposal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ctc.execution.automation import (
    AutomaticTransition,
    BotOrderRegistry,
    OrderRegistryError,
    SessionPnLTracker,
)
from ctc.execution.client import ExchangeClient, ExchangeError
from ctc.execution.log import get_logger
from ctc.execution.models import OrderRequest, OrderType, Side
from ctc.execution.risk import RiskManager, gross_exposure
from ctc.execution.settings import LiveConfig
from ctc.execution.state import LiveState
from ctc.strategy.signals import StrategyConfig, decide_live

log = get_logger("trader")


@dataclass
class ProposalLog:
    """Full record of one decision cycle, for observability (dry-run inspection)."""

    approved: list[OrderRequest] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason)
    n_candidates: int = 0


@dataclass
class LiveTrader:
    client: ExchangeClient
    state: LiveState
    risk: RiskManager
    cfg: LiveConfig
    strat: StrategyConfig = field(default_factory=StrategyConfig)
    order_registry: BotOrderRegistry = field(default_factory=BotOrderRegistry)
    automatic_live_authorized: bool = False
    last_reconcile_ok: bool = False
    execution_fault: bool = False

    # churn control: last proposed limit price per symbol
    _last_price: dict[str, float] = field(default_factory=dict)
    _submitted_order_ids: set[str] = field(default_factory=set)
    n_submitted: int = 0
    n_dry_run: int = 0

    # ---- decision stage (pure, no I/O) --------------------------------
    def propose(self) -> ProposalLog:
        plog = ProposalLog()
        fair, unc = self.state.fair_values_with_uncertainty()
        books = self.state.executable()
        mids = self.state.mids()
        now = self.state.clock()
        stale = set(self.state.stale_symbols(self.cfg.stale_after_s, now))
        resting = self.state.resting_exposure()
        dedup_keys = {
            OrderRequest(
                symbol=o.symbol,
                side=o.side,
                quantity=max(1, int(o.remaining)),
                order_type=OrderType.LIMIT,
                price=o.price,
            ).dedup_key()
            for o in self.state.open_orders.values()
        }
        gross = gross_exposure(self.state.positions, mids)

        tradable = None
        if self.strat.gate_until_report and not self.state.estimator.reported_rounds:
            tradable = {s for s in self.state.positions if s not in self.strat.gate_until_report}
        decisions = decide_live(fair, books, self.state.positions, self.strat, unc, tradable=tradable)
        for d in decisions:
            if d.order == 0:
                continue
            plog.n_candidates += 1
            sym = d.symbol
            # churn control: skip if price barely moved vs the last proposal for this symbol
            last = self._last_price.get(sym)
            if last is not None and abs(d.executable - last) < self.cfg.min_reprice_edge:
                plog.rejected.append((sym, "unchanged_signal"))
                continue
            req = OrderRequest(
                symbol=sym,
                side=Side.of_order(d.order),
                quantity=abs(d.order),
                order_type=OrderType.LIMIT,
                price=round(d.executable, 6),
                client_tag=f"{sym}:{d.order}",
            )
            rd = self.risk.evaluate(
                req,
                position=self.state.positions.get(sym, 0),
                resting_signed=resting.get(sym, 0),
                mid=mids.get(sym),
                gross_exposure=gross,
                stale=sym in stale,
                existing_dedup_keys=dedup_keys,
            )
            if rd.approved and rd.order is not None:
                plog.approved.append(rd.order)
                gross += rd.order.quantity * (mids.get(sym) or 0.0)
                dedup_keys.add(rd.order.dedup_key())
                self._last_price[sym] = rd.order.price or d.executable
            else:
                plog.rejected.append((sym, rd.reason))
        return plog

    # ---- execution stage (I/O; gated) ---------------------------------
    async def execute(self, plog: ProposalLog) -> None:
        if not self.cfg.dry_run and not self.state.market_is_running(self.cfg.stale_after_s):
            if plog.approved:
                log.warning("live order gate: exchange status is not freshly confirmed as running")
            return
        for order in plog.approved:
            # CLI live flags are only the outer opt-in. Even then, this process submits
            # only after the in-process transition controller reports every gate green.
            auto_gate_closed = not self.automatic_live_authorized
            if self.cfg.dry_run or self.cfg.kill_switch or auto_gate_closed:
                self.n_dry_run += 1
                log.info(
                    "DRY-RUN would submit: %s %s x%d @ %s",
                    order.symbol,
                    order.side.value,
                    order.quantity,
                    order.price,
                )
                continue
            intent_id: str | None = None
            try:
                intent_id = self.order_registry.begin_submission(order)
                ack = await self.client.place_order(order)
                self.order_registry.record_ack(intent_id, ack)
                if ack.status and ack.status.lower() in {"rejected", "error", "failed"}:
                    self.execution_fault = True
                    self.automatic_live_authorized = False
                    self.engage_kill_switch()
                    log.error("exchange returned a non-success order status for %s", order.symbol)
                    continue
                self.n_submitted += 1
                if ack.order_id:
                    self._submitted_order_ids.add(ack.order_id)
                log.info("SUBMITTED %s -> order_id=%s status=%s", order.symbol, ack.order_id, ack.status)
            except Exception as exc:  # noqa: BLE001 - preserve runner; ambiguous writes fail closed
                # A 4xx response is a definite rejection; a 5xx or transport failure may
                # follow an accepted write and stays unresolved for restart reconciliation.
                if intent_id is not None:
                    try:
                        self.order_registry.record_failure(
                            intent_id,
                            definite_rejection=isinstance(exc, ExchangeError) and 400 <= exc.status < 500,
                        )
                    except Exception:  # noqa: BLE001 - preserve the original unresolved intent
                        pass
                self.execution_fault = True
                self.automatic_live_authorized = False
                self.engage_kill_switch()
                log.error("order execution failed for %s (%s)", order.symbol, type(exc).__name__)

    async def step(self) -> ProposalLog:
        plog = self.propose()
        await self.execute(plog)
        return plog

    # ---- reconciliation + shutdown ------------------------------------
    async def reconcile(self) -> None:
        """Pull authoritative positions and open orders from the exchange."""
        try:
            positions = await self.client.get_positions()
            self.state.reconcile_positions(positions)
            orders = await self.client.get_open_orders()
            self.state.reconcile_open_orders(orders)
            owned_ok = self.order_registry.reconcile(orders)
            self.last_reconcile_ok = owned_ok
            if owned_ok:
                self._submitted_order_ids.update(self.order_registry.known_open_ids())
            else:
                log.error("order ownership reconciliation incomplete; unknown orders will not be cancelled")
            log.info("reconciled: %d positions, %d open orders", len(positions), len(orders))
        except (ExchangeError, OrderRegistryError) as exc:
            self.last_reconcile_ok = False
            log.warning("reconcile failed: %s", exc)

    async def enforce_session_loss(self, tracker: SessionPnLTracker) -> bool:
        """Latch the hard stop and cancel only IDs recorded from our own order ACKs.

        Incomplete PnL input blocks automatic execution but is not interpreted as zero loss.
        This method never liquidates positions and never cancels unregistered orders.
        """
        if not tracker.loss_limit_reached:
            return False
        self.engage_kill_switch()
        for order_id in tuple(self._submitted_order_ids):
            if order_id not in self.order_registry.orders:
                continue
            try:
                await self.client.cancel_order(order_id)
                self.order_registry.record_cancel(order_id, confirmed=True)
                self._submitted_order_ids.discard(order_id)
                log.warning("loss stop cancelled bot-owned order %s", order_id)
            except Exception as exc:  # noqa: BLE001 - continue monitoring after cancellation faults
                log.error(
                    "loss stop could not confirm cancellation for bot order %s (%s)",
                    order_id,
                    type(exc).__name__,
                )
        return True

    def authorize_resume_after_loss(
        self,
        tracker: SessionPnLTracker,
        transition: AutomaticTransition,
        *,
        manual_authorization: bool,
    ) -> None:
        """Resume only after an explicit operator action and a fresh PnL snapshot."""
        if not manual_authorization:
            raise PermissionError("manual authorization is required after a loss-limit stop")
        tracker.authorize_resume(manual_authorization=True)
        transition.authorize_resume(manual_authorization=True)
        self.cfg.kill_switch = False
        self.risk.kill_switch = False
        self.automatic_live_authorized = False
        log.warning("manual resume accepted; returning to dry-run rollout checks")

    async def shutdown(self, cancel_orders: bool = True) -> None:
        """Safe shutdown: optionally cancel all resting orders (never in dry-run)."""
        if cancel_orders and not self.cfg.dry_run:
            for order_id in tuple(self._submitted_order_ids):
                try:
                    await self.client.cancel_order(order_id)
                    if order_id in self.order_registry.orders:
                        self.order_registry.record_cancel(order_id, confirmed=True)
                    log.info("cancelled bot-owned order %s on shutdown", order_id)
                except Exception as exc:  # noqa: BLE001 - keep safe shutdown progressing
                    log.warning("cancel of bot-owned order %s failed (%s)", order_id, type(exc).__name__)
        else:
            log.info("shutdown: dry_run/kill_switch -> no cancels sent")

    def engage_kill_switch(self) -> None:
        self.cfg.kill_switch = True
        self.risk.kill_switch = True
        log.warning("KILL SWITCH ENGAGED: no new orders will be proposed or submitted")

    def status(self) -> dict[str, Any]:
        return {
            "dry_run": self.cfg.dry_run,
            "kill_switch": self.cfg.kill_switch,
            "submitted": self.n_submitted,
            "dry_run_decisions": self.n_dry_run,
            "automatic_live_authorized": self.automatic_live_authorized,
            "execution_fault": self.execution_fault,
            **self.state.summary(),
        }
