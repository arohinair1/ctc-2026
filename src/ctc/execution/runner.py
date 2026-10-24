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
import json
import time
from pathlib import Path

from ctc.config import ALL_SYMBOLS
from ctc.execution.client import ExchangeClient
from ctc.execution.log import get_logger, setup_logging
from ctc.execution.recorder import iter_recorded_frames
from ctc.execution.risk import RiskManager
from ctc.execution.settings import LiveConfig
from ctc.execution.state import LiveState
from ctc.execution.trader import LiveTrader
from ctc.forecasts.estimator import build_live_estimator

log = get_logger("runner")


def _build_trader(cfg: LiveConfig, client: ExchangeClient) -> LiveTrader:
    estimator = build_live_estimator()
    state = LiveState(estimator)
    risk = RiskManager(cfg.max_order_size_frac, cfg.max_gross_exposure, cfg.kill_switch)
    return LiveTrader(client, state, risk, cfg)


# ---------------------------------------------------------------------------
# ctc-bot — live loop
# ---------------------------------------------------------------------------
async def run_bot(
    cfg: LiveConfig,
    *,
    step_interval_s: float = 5.0,
    reconcile_every_s: float = 20.0,
    status_poll_s: float = 5.0,
    recording_path: str | Path | None = None,
) -> None:
    client = ExchangeClient(cfg)
    trader = _build_trader(cfg, client)
    log.info("starting bot | %s", json.dumps(cfg.redacted()))
    if cfg.dry_run:
        log.warning("DRY-RUN mode: decisions are logged but NO orders will be submitted")

    async def poll_status() -> None:
        previous = (trader.state.competition_id, trader.state.exchange_state)
        try:
            info = await client.get_time()
        except Exception as exc:  # noqa: BLE001 - fail closed for live order execution
            trader.state.exchange_state = None
            trader.state.exchange_state_checked_at = None
            log.warning("competition status poll failed (%s)", type(exc).__name__)
            return
        changed_season = trader.state.update_exchange_status(info)
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
            await trader.reconcile()

    await poll_status()
    await trader.reconcile()
    channels = ["news", "quotes", "trades", "orderbook"]
    last_step = 0.0
    last_reconcile = time.monotonic()
    last_status_poll = time.monotonic()
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
                    await trader.reconcile()
            if now - last_status_poll >= status_poll_s:
                await poll_status()
                last_status_poll = time.monotonic()
            if now - last_step >= step_interval_s:
                plog = await trader.step()
                last_step = now
                if plog.approved:
                    log.info("cycle: %d approved, %d rejected", len(plog.approved), len(plog.rejected))
            if now - last_reconcile >= reconcile_every_s:
                await trader.reconcile()
                last_reconcile = now
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("interrupted; shutting down")
    finally:
        if not next_frame.done():
            next_frame.cancel()
            await asyncio.gather(next_frame, return_exceptions=True)
        if record_file is not None:
            record_file.close()
        await stream.aclose()
        await trader.shutdown()
        log.info("final status: %s", json.dumps(trader.status()))


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
    if args.live and cfg.dry_run:
        log.error("--live passed but CTC_DRY_RUN still true; refusing. Set CTC_DRY_RUN=false to confirm.")
        return 2
    if not cfg.has_key():
        log.error("no CTC_API_KEY found (set it in .env)")
        return 2
    asyncio.run(run_bot(cfg, step_interval_s=args.step_interval, recording_path=args.recording))
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
