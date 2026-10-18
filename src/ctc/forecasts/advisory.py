"""Measure the information quality of official advisories and wire chatter.

All functions respect the live/hindsight distinction: an advisory at storm age ``a``
is scored against the pattern that was *true at age a* (accounting for a later turn
to erratic), never against information that only arrived afterwards.

Deliverables (case packet "Before the event"):
* overall accuracy of each source at naming the true pattern,
* accuracy as a function of storm age,
* how early each source first (and durably) identifies the eventual pattern,
* confusion matrices per source,
* calibration of the official per-island hurricane-condition probabilities,
* whether wire chatter adds information beyond the official advisory.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ctc.config import ISLANDS, OUTLOOKS
from ctc.data.history import History


def truth_at_age(storm: dict[str, Any], age: float) -> str:
    """The pattern that is true at storm ``age`` (erratic once the storm has turned)."""
    flip = storm["turned_erratic_at"]
    if flip is not None and age >= flip:
        return "erratic"
    return storm["pattern"]


def _storm_by_round(history: History):
    for season in history.seasons:
        rmap = {s["round"]: s for s in season.storms}
        yield season, rmap


@dataclass
class AccuracyResult:
    overall: dict[str, float]
    by_age: dict[str, dict[tuple[int, int], float]]
    counts: dict[str, int]


def accuracy(
    history: History, age_bins: tuple[int, ...] = (0, 60, 120, 180, 240, 300, 10_000)
) -> AccuracyResult:
    """Overall and age-bucketed accuracy of each advisory source at naming the pattern."""
    right = defaultdict(int)
    total = defaultdict(int)
    bin_right: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    bin_total: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for season, rmap in _storm_by_round(history):
        for adv in season.messages(type="advisory"):
            storm = rmap[adv["round"]]
            src = adv["source"]
            age = adv.get("age", 0)
            correct = int(adv["outlook"] == truth_at_age(storm, age))
            right[src] += correct
            total[src] += 1
            for lo, hi in zip(age_bins, age_bins[1:], strict=False):
                if lo <= age < hi:
                    bin_right[src][(lo, hi)] += correct
                    bin_total[src][(lo, hi)] += 1
                    break
    overall = {s: right[s] / total[s] for s in total}
    by_age = {
        s: {b: bin_right[s][b] / bin_total[s][b] for b in bin_total[s] if bin_total[s][b]} for s in bin_total
    }
    return AccuracyResult(overall, by_age, dict(total))


def earliness(history: History) -> dict[str, dict[str, float]]:
    """How early each source identifies the eventual (final) pattern.

    For each storm and source, find the earliest advisory age from which the source's
    outlook equals the storm's *final* pattern and stays correct for the rest of the
    storm. Returns mean/median of that age per source (``inf`` storms are skipped and
    counted as ``never_frac``).
    """
    per_source: dict[str, list[float]] = defaultdict(list)
    never: dict[str, int] = defaultdict(int)
    storms_count = 0
    for season, rmap in _storm_by_round(history):
        for r, storm in rmap.items():
            final = truth_at_age(storm, 10_000)
            storms_count += 1
            for src in ("official", "wire"):
                advs = sorted(season.messages(r, type="advisory", source=src), key=lambda m: m.get("age", 0))
                first_durable = None
                for i, a in enumerate(advs):
                    if a["outlook"] == final and all(b["outlook"] == final for b in advs[i:]):
                        first_durable = a.get("age", 0)
                        break
                if first_durable is None:
                    never[src] += 1
                else:
                    per_source[src].append(first_durable)
    import numpy as np

    out = {}
    for src in ("official", "wire"):
        ages = per_source[src]
        out[src] = {
            "mean_age": float(np.mean(ages)) if ages else float("inf"),
            "median_age": float(np.median(ages)) if ages else float("inf"),
            "never_frac": never[src] / storms_count if storms_count else 0.0,
            "n_identified": len(ages),
        }
    return out


def confusion_matrix(history: History, source: str) -> dict[str, dict[str, int]]:
    """Confusion matrix (true pattern -> predicted outlook) for a source."""
    mat = {t: dict.fromkeys(OUTLOOKS, 0) for t in OUTLOOKS}
    for season, rmap in _storm_by_round(history):
        for adv in season.messages(type="advisory", source=source):
            storm = rmap[adv["round"]]
            t = truth_at_age(storm, adv.get("age", 0))
            mat[t][adv["outlook"]] += 1
    return mat


@dataclass
class Calibration:
    bins: list[tuple[float, float]]
    predicted: list[float] = field(default_factory=list)
    observed: list[float] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)


def hurricane_condition_calibration(history: History, n_bins: int = 10) -> Calibration:
    """Calibrate official per-island hurricane-condition probabilities.

    For each official advisory and each island, pair the forecast probability with the
    realized outcome: whether that island reported hurricane conditions (an ``impact``
    message) in that round at age >= the advisory age ("during the remainder").
    """
    import numpy as np

    edges = np.linspace(0, 1, n_bins + 1)
    pred_sum = np.zeros(n_bins)
    obs_sum = np.zeros(n_bins)
    cnt = np.zeros(n_bins)
    for season in history.seasons:
        # impacts per round/island: earliest impact age
        impacts: dict[tuple[int, str], float] = {}
        for m in season.messages(type="impact"):
            key = (m["round"], m["island"])
            impacts[key] = min(impacts.get(key, 1e9), m.get("age", 0))
        for adv in season.messages(type="advisory", source="official"):
            r = adv["round"]
            for isl in ISLANDS:
                p = (adv.get("hurricane_conditions") or {}).get(isl)
                if p is None:
                    continue
                impact_age = impacts.get((r, isl))
                realized = 1.0 if (impact_age is not None) else 0.0
                b = min(int(p * n_bins), n_bins - 1)
                pred_sum[b] += p
                obs_sum[b] += realized
                cnt[b] += 1
    cal = Calibration(bins=list(zip(edges[:-1], edges[1:], strict=False)))
    for i in range(n_bins):
        if cnt[i]:
            cal.predicted.append(float(pred_sum[i] / cnt[i]))
            cal.observed.append(float(obs_sum[i] / cnt[i]))
            cal.counts.append(int(cnt[i]))
        else:
            cal.predicted.append(float("nan"))
            cal.observed.append(float("nan"))
            cal.counts.append(0)
    return cal


def wire_adds_information(history: History) -> dict[str, float]:
    """Does the wire add predictive value beyond the latest official advisory?

    Compares, over all times a wire rumor exists alongside a prior official advisory:
    the accuracy of the official alone, the wire alone, and the simple rule "trust the
    wire only when it agrees with the official". Also reports how often the wire
    disagrees with the official and, when it does, which one is right.
    """
    off_right = off_tot = 0
    wire_right = wire_tot = 0
    agree = disagree = 0
    wire_right_on_disagree = off_right_on_disagree = 0
    for season, rmap in _storm_by_round(history):
        for r, storm in rmap.items():
            officials = sorted(
                season.messages(r, type="advisory", source="official"), key=lambda m: m.get("age", 0)
            )
            for wire in season.messages(r, type="advisory", source="wire"):
                age = wire.get("age", 0)
                prior = [o for o in officials if o.get("age", 0) <= age]
                if not prior:
                    continue
                off = prior[-1]
                t = truth_at_age(storm, age)
                off_c = int(off["outlook"] == t)
                wire_c = int(wire["outlook"] == t)
                off_right += off_c
                off_tot += 1
                wire_right += wire_c
                wire_tot += 1
                if off["outlook"] == wire["outlook"]:
                    agree += 1
                else:
                    disagree += 1
                    wire_right_on_disagree += wire_c
                    off_right_on_disagree += off_c
    return {
        "official_accuracy": off_right / off_tot if off_tot else float("nan"),
        "wire_accuracy": wire_right / wire_tot if wire_tot else float("nan"),
        "agree_frac": agree / (agree + disagree) if (agree + disagree) else float("nan"),
        "on_disagree_wire_right": wire_right_on_disagree / disagree if disagree else float("nan"),
        "on_disagree_official_right": off_right_on_disagree / disagree if disagree else float("nan"),
        "n_pairs": off_tot,
    }
