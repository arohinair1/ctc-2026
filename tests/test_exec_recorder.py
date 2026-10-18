"""Tests for feed recording/replay helpers."""

from __future__ import annotations

import json

from ctc.execution.recorder import iter_recorded_frames, recording_gaps


def _write(tmp_path, lines):
    p = tmp_path / "rec.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return p


def test_raw_feed_message_wrapped_as_news(tmp_path):
    p = _write(tmp_path, [{"type": "damage_report", "round": 1, "seq": 1, "damage_pct": {"PORT": 10.0}}])
    frames = list(iter_recorded_frames(p))
    assert len(frames) == 1
    f = frames[0]
    assert f.type == "news"
    assert f.message["type"] == "damage_report"  # original message preserved inside


def test_top_level_frames_pass_through(tmp_path):
    p = _write(tmp_path, [{"type": "quote", "symbol": "PORT", "bid": 1.0, "ask": 2.0, "received_at": 5.0}])
    frames = list(iter_recorded_frames(p))
    assert frames[0].type == "quote"
    assert frames[0].received_at == 5.0


def test_recording_gaps_detects_missing_and_duplicate_seq(tmp_path):
    p = _write(
        tmp_path,
        [
            {"type": "news", "message": {"type": "storm", "seq": 1}},
            {"type": "news", "message": {"type": "storm", "seq": 3}},  # seq 2 missing
            {"type": "news", "message": {"type": "storm", "seq": 3}},  # duplicate
        ],
    )
    gaps = recording_gaps(p)
    assert gaps["news_with_seq"] == 2
    assert gaps["missing"] == [2]
    assert gaps["duplicates"] == 1
