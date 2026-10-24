"""Entry points for the live bot, the connectivity probe, and offline replay.

* ``ctc-bot``        — run the trading loop (DRY_RUN by default; never submits unless
  ``CTC_DRY_RUN=false`` is set explicitly).
* ``ctc-bot-probe``  — read-only connectivity check (public + authenticated endpoints and,
  optionally, a brief WebSocket listen). Never places orders.
* ``ctc-bot-replay`` — drive the full pipeline from a recording or a research ``feed.jsonl``
  in dry-run and print the generated signals.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from ctc.config import ALL_SYMBOLS
from ctc.execution.automation import (
    AutomaticTransition,
    BotOrderRegistry,
    ExecutionTestEvidence,
    ProcessLease,
    SafetySnapshot,
    SessionPnLTracker,
    TransitionPhase,
    VerifiedPnLSnapshot,
)
from ctc.execution.client import ExchangeClient
from ctc.execution.log import get_logger, setup_logging
from ctc.execution.models import OrderRequest, OrderType, Side
from ctc.execution.recorder import iter_recorded_frames
from ctc.execution.risk import RiskManager, gross_exposure
from ctc.execution.settings import LiveConfig
from ctc.execution.state import LiveState
from ctc.execution.trader import LiveTrader
from ctc.forecasts.estimator import build_live_estimator

log = get_logger("runner")


def _build_trader(cfg: LiveConfig, client: ExchangeClient) -> LiveTrader:
    estimator = build_live_estimator()
    state = LiveState(estimator)
    order_frac = cfg.max_order_size_frac
    gross_cap = cfg.max_gross_exposure
    if cfg.automatic_transition_enabled:
        order_frac = min(order_frac, cfg.initial_max_order_size_frac)
        gross_cap = min(gross_cap, cfg.max_initial_gross_exposure)
    initial_order_units = cfg.initial_max_order_size_units if cfg.automatic_transition_enabled else None
    risk = RiskManager(order_frac, gross_cap, cfg.kill_switch, initial_order_units)
    registry = BotOrderRegistry(cfg.order_registry_path)
    return LiveTrader(client, state, risk, cfg, order_registry=registry)


AccountingProvider = Callable[[ExchangeClient], Awaitable[VerifiedPnLSnapshot]]


def _load_accounting_provider(spec: str) -> AccountingProvider | None:
    """Load a team-owned async ``module:function`` accounting adapter, if configured."""
    if not spec:
        return None
    module_name, separator, function_name = spec.partition(":")
    if not separator or not module_name.strip() or not function_name.strip():
        raise ValueError("CTC_ACCOUNTING_PROVIDER must use module:function syntax")
    provider = getattr(importlib.import_module(module_name.strip()), function_name.strip())
    if not callable(provider):
        raise TypeError("configured accounting provider is not callable")
    return provider


async def _run_port_execution_test(
    trader: LiveTrader,
    *,
    timeout_s: float = 3.0,
    poll_interval_s: float = 0.1,
) -> ExecutionTestEvidence | None:
    """Run the pre-authorized one-share PORT passive place/cancel round trip.

    Returns None without writing if exchange/quote/ownership gates are not currently safe.
    Once the durable one-shot marker is written, every incomplete outcome is returned as
    failed evidence so the controller enters SAFE_MODE instead of retrying blindly.
    """
    now = time.time()
    quote = trader.state.quotes.get("PORT")
    if (
        trader.cfg.dry_run
        or not trader.cfg.automatic_transition_enabled
        or not trader.state.market_is_running(trader.cfg.stale_after_s)
        or not trader.last_reconcile_ok
        or not trader.order_registry.ownership_verified
        or quote is None
        or quote.bid is None
        or quote.ask is None
        or not math.isfinite(quote.bid)
        or not math.isfinite(quote.ask)
        or quote.bid <= 0
        or quote.ask <= quote.bid
        or now - quote.received_at < 0
        or now - quote.received_at > trader.cfg.stale_after_s
    ):
        return None

    try:
        current_orders = await trader.client.get_open_orders()
        if any(order.symbol == "PORT" for order in current_orders):
            return None
        if not trader.order_registry.reconcile(current_orders):
            return None
        current_positions = await trader.client.get_positions()
    except Exception as exc:  # noqa: BLE001 - don't attempt writes without readback health
        log.warning("PORT execution test waiting for read APIs (%s)", type(exc).__name__)
        return None

    position_before = next((p.quantity for p in current_positions if p.symbol == "PORT"), 0)
    state_position = trader.state.positions.get("PORT", 0)
    if position_before != state_position:
        log.warning("PORT execution test waiting for position state to match exchange reconciliation")
        return None
    best_bid = quote.bid
    test_price = round(best_bid * 0.5, 6)
    if test_price <= 0 or test_price >= best_bid:
        return None
    request = OrderRequest(
        symbol="PORT",
        side=Side.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        price=test_price,
        client_tag=f"ctc-port-rollout-test-{uuid.uuid4()}",
    )
    mid = (quote.bid + quote.ask) / 2
    risk = trader.risk.evaluate(
        request,
        position=position_before,
        resting_signed=0,
        mid=mid,
        gross_exposure=gross_exposure(trader.state.positions, trader.state.mids()),
        stale=False,
    )
    if not risk.approved or risk.order is None or risk.order.quantity != 1:
        return None

    trader.order_registry.begin_execution_test()
    intent_id = trader.order_registry.begin_submission(request)
    ack = None
    order_id = ""
    appeared = False
    cancel_acknowledged = False
    absent_after_cancel = False
    fill_quantity = 0.0
    failure = False
    try:
        ack = await trader.client.place_order(request)
        trader.order_registry.record_ack(intent_id, ack)
        order_id = ack.order_id or ""
        fill_quantity = ack.filled
        if not order_id:
            failure = True
        else:
            trader._submitted_order_ids.add(order_id)
            deadline = asyncio.get_running_loop().time() + max(0.0, timeout_s)
            while True:
                remote_orders = await trader.client.get_open_orders()
                if not trader.order_registry.reconcile(remote_orders):
                    failure = True
                matching = next((o for o in remote_orders if o.order_id == order_id), None)
                if matching is not None:
                    appeared = (
                        matching.symbol == "PORT"
                        and matching.side is Side.BUY
                        and matching.quantity == 1
                        and matching.price is not None
                        and math.isclose(matching.price, test_price, abs_tol=1e-6)
                    )
                    fill_quantity = max(fill_quantity, matching.filled)
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(max(0.01, poll_interval_s))

            try:
                await trader.client.cancel_order(order_id)
                cancel_acknowledged = True
                trader.order_registry.record_cancel(order_id, confirmed=True)
            except Exception:  # noqa: BLE001 - failed cancellation must stop rollout
                failure = True
                try:
                    trader.order_registry.record_cancel(order_id, confirmed=False)
                except Exception:  # noqa: BLE001 - retain the original failure
                    pass
            trader._submitted_order_ids.discard(order_id)

            deadline = asyncio.get_running_loop().time() + max(0.0, timeout_s)
            while True:
                remote_orders = await trader.client.get_open_orders()
                if not trader.order_registry.reconcile(remote_orders):
                    failure = True
                if all(o.order_id != order_id for o in remote_orders):
                    absent_after_cancel = True
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(max(0.01, poll_interval_s))

        positions_after = await trader.client.get_positions()
        position_after = next((p.quantity for p in positions_after if p.symbol == "PORT"), 0)
    except Exception as exc:  # noqa: BLE001 - persist failure and fail closed
        log.error("PORT execution test failed (%s)", type(exc).__name__)
        failure = True
        position_after = position_before
        if order_id:
            try:
                await trader.client.cancel_order(order_id)
                cancel_acknowledged = True
                trader.order_registry.record_cancel(order_id, confirmed=True)
                trader._submitted_order_ids.discard(order_id)
            except Exception:  # noqa: BLE001 - ownership remains unresolved
                try:
                    trader.order_registry.record_cancel(order_id, confirmed=False)
                except Exception:  # noqa: BLE001
                    pass
    if ack is None or not ack.status or ack.status.lower() in {"rejected", "error", "failed"}:
        failure = True
    if not cancel_acknowledged or not appeared or not absent_after_cancel:
        failure = True
    evidence = ExecutionTestEvidence(
        order_id=order_id,
        symbol="PORT",
        side="buy",
        quantity=1,
        limit_price=test_price,
        best_bid_before=best_bid,
        fill_quantity=fill_quantity,
        appeared_in_open_orders=appeared,
        cancellation_acknowledged=cancel_acknowledged,
        absent_after_cancel=absent_after_cancel,
        port_position_before=position_before,
        port_position_after=position_after,
        competition_id=trader.state.competition_id,
        execution_integrity=not failure,
    )
    trader.order_registry.record_execution_test_result(asdict(evidence))
    if failure:
        trader.execution_fault = True
    return evidence


# ---------------------------------------------------------------------------
# ctc-bot — live loop
# ---------------------------------------------------------------------------
async def run_bot(
    cfg: LiveConfig,
    *,
    step_interval_s: float = 5.0,
    reconcile_every_s: float = 5.0,
    status_poll_s: float = 5.0,
    recording_path: str | Path | None = None,
    accounting_provider: AccountingProvider | None = None,
) -> None:
    process_lease = ProcessLease(cfg.process_lock_path)
    try:
        lease_acquired = process_lease.acquire()
    except OSError as exc:
        log.error("could not acquire singleton bot lock (%s); refusing to connect", type(exc).__name__)
        return
    if not lease_acquired:
        log.error("another bot process holds the singleton lock; refusing duplicate connection")
        return
    client = ExchangeClient(cfg)
    trader = _build_trader(cfg, client)
    transition = AutomaticTransition(
        live_activation_enabled=cfg.automatic_transition_enabled and not cfg.dry_run
    )
    pnl_tracker = SessionPnLTracker(
        Path(cfg.session_pnl_path), loss_limit=cfg.max_session_loss, max_age_s=cfg.stale_after_s
    )
    if accounting_provider is None:
        log.warning("verified account PnL provider is unavailable; session-loss and live gates remain closed")
    log.info("starting bot | %s", json.dumps(cfg.redacted()))
    if cfg.dry_run:
        log.warning("DRY-RUN mode: decisions are logged but NO orders will be submitted")
    elif cfg.automatic_transition_enabled:
        log.warning("automatic rollout armed; submissions remain gated until all checks pass")

    status_healthy = False
    execution_api_healthy = False

    async def poll_status() -> None:
        nonlocal status_healthy, execution_api_healthy
        previous = (trader.state.competition_id, trader.state.exchange_state)
        try:
            info = await client.get_time()
        except Exception as exc:  # noqa: BLE001 - fail closed for live order execution
            status_healthy = False
            execution_api_healthy = False
            trader.automatic_live_authorized = False
            trader.state.exchange_state = None
            trader.state.exchange_state_checked_at = None
            log.warning("competition status poll failed (%s)", type(exc).__name__)
            return
        status_healthy = True
        execution_api_healthy = True
        changed_season = trader.state.update_exchange_status(info)
        if cfg.automatic_transition_enabled and not trader.state.market_is_running(cfg.stale_after_s):
            trader.automatic_live_authorized = False
        current = (trader.state.competition_id, trader.state.exchange_state)
        if current != previous:
            log.info(
                "competition status: id=%s state=%s fictional_time=%s",
                current[0],
                current[1],
                info.get("fictional_time"),
            )
        if changed_season:
            log.info("competition id changed; local season state reset")
            trader.order_registry.reset_execution_test()
            transition.execution_test_passed = False
            await reconcile_accounting()

    async def reconcile_accounting() -> None:
        """Apply trusted fills before open-order reconciliation, then refresh session PnL."""
        snapshot: VerifiedPnLSnapshot | None = None
        fills_complete = False
        if accounting_provider is not None:
            try:
                snapshot = await accounting_provider(client)
                fills_complete = trader.order_registry.reconcile_executions(
                    snapshot.executions,
                    history_complete=snapshot.execution_history_complete,
                )
            except Exception as exc:  # noqa: BLE001 - accounting failure fails closed
                log.warning("verified account snapshot unavailable (%s)", type(exc).__name__)
                snapshot = None
        await trader.reconcile()
        if snapshot is None:
            pnl_tracker.update(
                starting_equity=None,
                realized_pnl=None,
                unrealized_pnl=None,
                source_verified=False,
            )
            return
        now = pnl_tracker.clock()
        try:
            as_of = float(snapshot.as_of)
            timestamp_valid = math.isfinite(as_of)
        except (TypeError, ValueError):
            timestamp_valid = False
            as_of = 0.0
        fresh_snapshot = timestamp_valid and -1.0 <= now - as_of <= pnl_tracker.max_age_s
        verified = (
            snapshot.source_verified
            and fresh_snapshot
            and snapshot.execution_history_complete
            and fills_complete
            and trader.last_reconcile_ok
            and trader.order_registry.ownership_verified
            and trader.order_registry.execution_records_complete
        )
        pnl_tracker.update(
            starting_equity=snapshot.starting_equity,
            realized_pnl=snapshot.realized_pnl,
            unrealized_pnl=snapshot.unrealized_pnl,
            source_verified=verified,
        )
        if not verified:
            log.warning("account PnL/fill reconciliation incomplete; automatic live gate remains closed")

    await poll_status()
    await reconcile_accounting()
    channels = ["news", "quotes", "trades", "orderbook"]
    last_step = 0.0
    last_reconcile = time.monotonic()
    last_status_poll = time.monotonic()
    latest_frame_at: float | None = None
    stream = client.stream(ALL_SYMBOLS, channels)
    next_frame = asyncio.create_task(anext(stream))
    record_file = None
    if recording_path is not None:
        try:
            out = Path(recording_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            record_file = out.open("a")
            log.info("recording market feed to %s", out)
        except OSError as exc:
            log.error("could not open recording path (%s); continuing without recording", type(exc).__name__)
    try:
        while True:
            now = time.monotonic()
            wait_s = max(0.0, status_poll_s - (now - last_status_poll))
            done, _ = await asyncio.wait({next_frame}, timeout=wait_s)
            now = time.monotonic()
            if next_frame in done:
                try:
                    frame = next_frame.result()
                except StopAsyncIteration:
                    break
                next_frame = asyncio.create_task(anext(stream))
                trader.state.ingest_frame(frame)
                if frame.type == "_reconnect":
                    latest_frame_at = None
                    trader.automatic_live_authorized = False
                else:
                    latest_frame_at = time.monotonic()
                if record_file is not None:
                    try:
                        event = {**frame.raw, "received_at": frame.received_at or time.time()}
                        record_file.write(json.dumps(event) + "\n")
                        record_file.flush()
                    except OSError as exc:
                        log.error("feed recording stopped after write error (%s)", type(exc).__name__)
                        record_file.close()
                        record_file = None
                if frame.type == "_reconnect":
                    await reconcile_accounting()
            if now - last_status_poll >= status_poll_s:
                await poll_status()
                last_status_poll = time.monotonic()
            if now - last_step >= step_interval_s:
                loss_stopped = await trader.enforce_session_loss(pnl_tracker)
                if cfg.automatic_transition_enabled and trader.automatic_live_authorized:
                    now_mono = time.monotonic()
                    fresh_all = all(
                        symbol in trader.state.quotes
                        and trader.state.quotes[symbol].bid is not None
                        and trader.state.quotes[symbol].ask is not None
                        and 0 <= time.time() - trader.state.quotes[symbol].received_at <= cfg.stale_after_s
                        for symbol in ALL_SYMBOLS
                    )
                    if (
                        loss_stopped
                        or not status_healthy
                        or not trader.state.market_is_running(cfg.stale_after_s)
                        or latest_frame_at is None
                        or now_mono - latest_frame_at > cfg.stale_after_s
                        or not fresh_all
                        or not pnl_tracker.complete
                        or not trader.order_registry.ownership_verified
                        or not trader.last_reconcile_ok
                        or trader.execution_fault
                    ):
                        trader.automatic_live_authorized = False
                plog = await trader.step()
                quote_now = time.time()
                fresh_symbols = frozenset(
                    sym
                    for sym, quote in trader.state.quotes.items()
                    if quote.bid is not None
                    and quote.ask is not None
                    and 0 <= quote_now - quote.received_at <= cfg.stale_after_s
                )
                snapshot = SafetySnapshot(
                    exchange_running=trader.state.market_is_running(cfg.stale_after_s),
                    websocket_healthy=(
                        latest_frame_at is not None
                        and time.monotonic() - latest_frame_at <= cfg.stale_after_s
                    ),
                    fresh_symbols=fresh_symbols,
                    execution_api_healthy=execution_api_healthy,
                    positive_net_edge=bool(plog.approved),
                    limits_enforced=(
                        trader.risk.max_gross_exposure > 0
                        and trader.risk.max_order_size_frac > 0
                        and cfg.max_orders_per_min > 0
                    ),
                    pnl_complete=pnl_tracker.complete,
                    loss_limit_clear=pnl_tracker.complete and not pnl_tracker.loss_limit_reached,
                    ownership_verified=trader.order_registry.ownership_verified,
                    executions_complete=trader.order_registry.execution_records_complete,
                    process_healthy=process_lease.held and not loss_stopped,
                    no_unexplained_discrepancy=(trader.last_reconcile_ok and not trader.execution_fault),
                )
                previous_phase = transition.phase
                transition.tick(snapshot, observe=True)
                if transition.phase is TransitionPhase.EXECUTION_TEST:
                    saved = trader.order_registry.execution_test_result
                    if saved is not None and not transition.execution_test_passed:
                        try:
                            evidence = ExecutionTestEvidence(**saved)
                            if evidence.competition_id != trader.state.competition_id:
                                transition.fail_execution_test(
                                    "stored PORT test belongs to a different competition"
                                )
                            else:
                                transition.record_execution_test(evidence)
                        except (TypeError, ValueError, RuntimeError):
                            transition.fail_execution_test("stored PORT test evidence is invalid")
                    elif (
                        trader.order_registry.execution_test_attempted
                        and not transition.execution_test_passed
                    ):
                        transition.fail_execution_test("previous PORT test attempt has no complete evidence")
                    elif transition._non_alpha_gates_pass(snapshot):
                        try:
                            evidence = await _run_port_execution_test(trader)
                        except Exception as exc:  # noqa: BLE001 - unexpected test faults fail closed
                            log.error("PORT execution test aborted (%s)", type(exc).__name__)
                            transition.fail_execution_test(
                                "PORT execution test aborted; operator review required"
                            )
                        else:
                            if evidence is not None:
                                transition.record_execution_test(evidence)
                trader.automatic_live_authorized = transition.phase is TransitionPhase.LIVE
                if transition.phase is not previous_phase:
                    log.warning(
                        "automatic transition: %s (%s)",
                        transition.phase.value,
                        transition.reason,
                    )
                last_step = now
                if plog.approved:
                    log.info("cycle: %d approved, %d rejected", len(plog.approved), len(plog.rejected))
            if now - last_reconcile >= reconcile_every_s:
                await reconcile_accounting()
                execution_api_healthy = status_healthy and trader.last_reconcile_ok
                last_reconcile = now
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("interrupted; shutting down")
    finally:
        try:
            if not next_frame.done():
                next_frame.cancel()
                await asyncio.gather(next_frame, return_exceptions=True)
            if record_file is not None:
                record_file.close()
            await stream.aclose()
            await trader.shutdown()
            final_status = trader.status()
            final_status["automatic_transition"] = transition.phase.value
            final_status["transition_reason"] = transition.reason
            final_status["session_pnl_complete"] = pnl_tracker.complete
            final_status["order_ownership_verified"] = trader.order_registry.ownership_verified
            log.info("final status: %s", json.dumps(final_status))
        finally:
            process_lease.release()


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Run the CTC live trading bot (dry-run by default).")
    parser.add_argument("--step-interval", type=float, default=5.0, help="seconds between decision cycles")
    parser.add_argument(
        "--recording",
        type=Path,
        default=Path("research/recordings/live.jsonl"),
        help="append raw feed frames to this gitignored JSONL file",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="DANGER: enable live order submission (also needs CTC_DRY_RUN=false)",
    )
    args = parser.parse_args()
    cfg = LiveConfig.from_env()
    if not args.live and not cfg.dry_run:
        log.error("CTC_DRY_RUN=false requires --live; refusing to start without both live gates.")
        return 2
    if args.live and cfg.dry_run:
        log.error("--live passed but CTC_DRY_RUN still true; refusing. Set CTC_DRY_RUN=false to confirm.")
        return 2
    if args.live and not cfg.automatic_transition_enabled:
        log.error("--live requires CTC_AUTO_TRANSITION=true; live submission stays gated by verified checks")
        return 2
    if not cfg.has_key():
        log.error("no CTC_API_KEY found (set it in .env)")
        return 2
    try:
        accounting_provider = _load_accounting_provider(cfg.accounting_provider)
    except Exception as exc:  # noqa: BLE001 - keep monitoring possible; the live gate fails closed
        log.error(
            "accounting provider could not be loaded (%s); verified PnL gate remains closed",
            type(exc).__name__,
        )
        accounting_provider = None
    asyncio.run(
        run_bot(
            cfg,
            step_interval_s=args.step_interval,
            recording_path=args.recording,
            accounting_provider=accounting_provider,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# ctc-bot-probe — read-only connectivity
# ---------------------------------------------------------------------------
async def run_probe(cfg: LiveConfig, *, listen_s: float = 0.0) -> dict:
    client = ExchangeClient(cfg)
    result = await client.probe()
    if listen_s > 0 and result.get("public_case", {}).get("ok"):
        frames: list[str] = []
        try:

            async def _listen() -> None:
                async for frame in client.stream(
                    ALL_SYMBOLS, ["news", "quotes", "trades", "orderbook"], max_reconnects=0
                ):
                    if frame.type != "_reconnect":
                        frames.append(frame.type)

            await asyncio.wait_for(_listen(), timeout=listen_s)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001
            result["ws"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        else:
            result["ws"] = {"ok": True, "frames": len(frames)}
        if "ws" not in result:
            result["ws"] = {"ok": bool(frames), "frames": len(frames)}
    return result


def probe_main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Read-only testnet connectivity probe (no orders).")
    parser.add_argument("--listen", type=float, default=0.0, help="also listen to the WS for N seconds")
    args = parser.parse_args()
    cfg = LiveConfig.from_env()
    log.info("probing %s", cfg.http_url)
    result = asyncio.run(run_probe(cfg, listen_s=args.listen))
    print(json.dumps(result, indent=2))
    ok = bool(result.get("public_case", {}).get("ok"))
    auth_ok = bool(result.get("auth_time", {}).get("ok"))
    if not auth_ok:
        log.warning("authenticated access failed — check that CTC_API_KEY is valid/active for the testnet")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# ctc-bot-replay — offline pipeline over a recording / research feed
# ---------------------------------------------------------------------------
def run_replay(cfg: LiveConfig, path: str, *, step_every: int = 50, verbose: bool = False) -> dict:
    cfg.dry_run = True  # replay never submits
    client = ExchangeClient(cfg)
    trader = _build_trader(cfg, client)
    # synthetic quotes are not in the research feed; seed flat quotes so decisions can form
    n = 0
    total_proposed = 0
    for frame in iter_recorded_frames(path):
        trader.state.ingest_frame(frame)
        n += 1
        if n % step_every == 0:
            plog = trader.propose()
            total_proposed += len(plog.approved)
            if verbose and plog.approved:
                for o in plog.approved:
                    log.info(
                        "replay signal: %s %s x%d @ %.4f",
                        o.symbol,
                        o.side.value,
                        o.quantity,
                        o.price or 0.0,
                    )
    return {"frames": n, "proposed_orders": total_proposed, **trader.state.summary()}


def replay_main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Replay a recording/feed through the pipeline (dry-run).")
    parser.add_argument("path", help="path to a recording.jsonl or a research feed.jsonl")
    parser.add_argument("--step-every", type=int, default=50)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    cfg = LiveConfig.from_env()
    result = run_replay(cfg, args.path, step_every=args.step_every, verbose=args.verbose)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
