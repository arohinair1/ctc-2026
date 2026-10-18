"""Feed recording and replay.

Recording writes one JSON frame per line, each annotated with ``received_at`` (unix
seconds) — byte-compatible with the starter kit's ``record_feed.py`` and therefore
verifiable with ``check_recording.py`` (which matches ``news`` frames by ``seq`` against
the research feed).

Replay yields :class:`~ctc.execution.models.Frame` objects through the *same* interface the
live bot consumes (:meth:`ctc.execution.state.LiveState.ingest_frame`), so a recorded
testnet session — or a raw research ``feed.jsonl`` — can drive the full pipeline offline.
Raw feed messages (research data) are transparently wrapped as ``news`` frames.

CLI: ``ctc-record --out recording.jsonl [--symbols PORT,HAVEN]``. Large recordings belong
outside git (``research/recordings/`` is gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path

from ctc.execution.client import ExchangeClient
from ctc.execution.log import get_logger, setup_logging
from ctc.execution.models import FEED_MESSAGE_TYPES, Frame
from ctc.execution.settings import LiveConfig

log = get_logger("recorder")

_TOP_LEVEL_FRAME_TYPES = {"news", "quote", "orderbook", "trade", "_reconnect"}


async def record(
    client: ExchangeClient,
    out_path: str | Path,
    symbols: list[str],
    channels: list[str],
    *,
    max_frames: int | None = None,
) -> int:
    """Stream frames into ``out_path`` (append). Returns the number of frames written."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("a") as f:
        async for frame in client.stream(symbols, channels):
            if frame.type == "_reconnect":
                continue
            f.write(json.dumps({**frame.raw, "received_at": frame.received_at or time.time()}) + "\n")
            f.flush()
            n += 1
            if max_frames is not None and n >= max_frames:
                break
    return n


def iter_recorded_frames(path: str | Path) -> Iterator[Frame]:
    """Yield frames from a recording or a raw research ``feed.jsonl``.

    Lines that are already frames (``type`` in news/quote/orderbook/...) pass through; raw
    feed messages (``type`` is a feed message type) are wrapped as ``news`` frames so they
    traverse the same code path as the live feed.
    """
    p = Path(path)
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            received_at = d.get("received_at", 0.0)
            t = d.get("type", "")
            if t in _TOP_LEVEL_FRAME_TYPES:
                yield Frame.parse(d, received_at=received_at)
            elif t in FEED_MESSAGE_TYPES:
                # raw feed message -> wrap as a news frame (mirrors the live wire format)
                yield Frame(type="news", raw={"type": "news", "message": d}, received_at=received_at)
            else:
                yield Frame.parse(d, received_at=received_at)


def recording_gaps(path: str | Path) -> dict[str, object]:
    """Report seq coverage of a recording (missing/duplicate seqs among news messages)."""
    seqs: list[int] = []
    dup = 0
    seen: set[int] = set()
    for frame in iter_recorded_frames(path):
        msg = frame.message
        if msg and isinstance(msg.get("seq"), int):
            s = msg["seq"]
            if s in seen:
                dup += 1
            else:
                seen.add(s)
                seqs.append(s)
    if not seqs:
        return {"news_with_seq": 0, "missing": [], "duplicates": dup}
    lo, hi = min(seqs), max(seqs)
    missing = sorted(set(range(lo, hi + 1)) - seen)
    return {
        "news_with_seq": len(seqs),
        "first_seq": lo,
        "last_seq": hi,
        "missing": missing,
        "n_missing": len(missing),
        "duplicates": dup,
    }


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Record the exchange market-data feed.")
    parser.add_argument("--out", default="research/recordings/recording.jsonl")
    parser.add_argument("--symbols", default="", help="comma-separated symbols for quotes/trades")
    parser.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args()

    cfg = LiveConfig.from_env()
    if not cfg.has_key():
        log.error("no CTC_API_KEY found (set it in .env); cannot record")
        return 2
    symbols = [s for s in args.symbols.split(",") if s]
    channels = ["news", "quotes", "trades"] if symbols else ["news"]
    client = ExchangeClient(cfg)
    log.info("recording to %s (channels=%s)", args.out, channels)
    n = 0
    with contextlib.suppress(KeyboardInterrupt):
        n = asyncio.run(record(client, args.out, symbols, channels, max_frames=args.max_frames))
    log.info("wrote %d frames to %s", n, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
