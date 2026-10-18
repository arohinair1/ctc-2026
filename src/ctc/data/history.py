"""Load the Hurricane Season research data.

This is a packaged, typed evolution of the competition starter kit's ``history.py``
(credited in docs/RESEARCH.md). It loads seasons lazily and exposes the feed, the
house consensus prices, the settlement values and the ground-truth manifest.

    from ctc.data.history import load
    data = load()                       # uses ctc.config.history_dir()
    season = data.seasons[0]
    season.storms                       # manifest entries (true pattern, damage, loss)
    season.messages(round=1, type="advisory", source="official")
    season.track(1)                     # [(age, x, y, radius), ...]
    season.prices["PORT"]               # house consensus price at each 2s sample
    season.settlement["PORT"]           # settlement value
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from ctc.config import history_dir


@dataclass
class Season:
    """One research season: three storms, the feed, the house prices and settlements."""

    number: int
    folder: Path
    storms: list[dict[str, Any]]
    season_loss_musd: float

    @cached_property
    def feed(self) -> list[dict[str, Any]]:
        """All feed messages in ``seq`` order, exactly as the exchange published them."""
        with (self.folder / "feed.jsonl").open() as f:
            return [json.loads(line) for line in f]

    @cached_property
    def times(self) -> list[str]:
        return [row["time"] for row in self._rows]

    @cached_property
    def rounds(self) -> list[int]:
        return [int(row["round"]) for row in self._rows]

    @cached_property
    def prices(self) -> dict[str, list[float]]:
        """House consensus price of each symbol at each 2s sample (delayed + small noise)."""
        symbols = [k for k in self._rows[0] if k not in ("time", "round")]
        return {s: [float(row[s]) for row in self._rows] for s in symbols}

    @cached_property
    def settlement(self) -> dict[str, float]:
        return json.loads((self.folder / "settlement.json").read_text())

    def messages(self, round: int | None = None, **match: Any) -> list[dict[str, Any]]:
        """Feed messages in ``round`` (or all rounds) matching every given field."""
        return [
            m
            for m in self.feed
            if (round is None or m.get("round") == round) and all(m.get(k) == v for k, v in match.items())
        ]

    def track(self, round: int) -> list[tuple[int, float, float, float]]:
        """Observed track of the storm in a round: ``(age, x, y, radius)`` per observation."""
        return [(m["age"], m["x"], m["y"], m["radius"]) for m in self.messages(round, type="storm")]

    def storm_of_round(self, round: int) -> dict[str, Any]:
        for s in self.storms:
            if s["round"] == round:
                return s
        raise KeyError(f"season {self.number} has no round {round}")

    @cached_property
    def _rows(self) -> list[dict[str, str]]:
        with (self.folder / "market.csv").open() as f:
            return list(csv.DictReader(f))


@dataclass
class History:
    """The research data: all seasons, in sequence."""

    root: Path
    seasons: list[Season] = field(default_factory=list)

    def season_of(self, date: str) -> Season | None:
        for s in self.seasons:
            if any(storm["open"].startswith(date) for storm in s.storms):
                return s
        return None

    def __len__(self) -> int:
        return len(self.seasons)


def load(root: str | Path | None = None) -> History:
    """Read ``manifest.json`` in ``root`` (defaults to :func:`ctc.config.history_dir`)."""
    root = Path(root) if root is not None else history_dir()
    manifest = json.loads((root / "manifest.json").read_text())
    seasons = [
        Season(s["season"], root / s["dir"], s["storms"], s["season_loss_musd"]) for s in manifest["seasons"]
    ]
    return History(root, seasons)
