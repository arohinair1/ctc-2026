"""Storm wind field and site-outage model.

The case packet states that wind falls off quickly with distance, that ``radius``
sets how far strong wind reaches and ``intensity`` sets its strength, and that a
site closes while its wind exceeds a threshold.

Calibrated against the 518+ ``impact`` events in the research feed (the first time
an island reports hurricane conditions), the relationship

    wind(site) = intensity * exp(-distance / radius)

is constant at impact with a coefficient of variation of ~10%, far tighter than a
linear or Gaussian falloff. The closing threshold is ~0.39. See docs/RESEARCH.md.

Outages are not reported in the data, so we reconstruct them from the public storm
observations (position every ~2 seconds). The site-weighted outage metric below
explains almost all of the GRID and FERRY settlement residual that damage alone
leaves (FERRY R^2 0.69 -> 0.99), confirming the model.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from ctc.config import WIND_CLOSE_THRESHOLD


def wind_at(x: float, y: float, obs: dict[str, Any]) -> float:
    """Wind at point ``(x, y)`` given a storm observation ``obs`` (keys x, y, radius, intensity)."""
    radius = obs["radius"]
    if radius <= 0:
        return 0.0
    dist = math.hypot(obs["x"] - x, obs["y"] - y)
    return obs["intensity"] * math.exp(-dist / radius)


def site_closed(x: float, y: float, obs: dict[str, Any], threshold: float = WIND_CLOSE_THRESHOLD) -> bool:
    return wind_at(x, y, obs) >= threshold


def _obs_spacing(obs_list: list[dict[str, Any]]) -> float:
    """Median spacing in storm-age seconds between consecutive observations (defaults to 2)."""
    ages = sorted(o["age"] for o in obs_list)
    diffs = [b - a for a, b in zip(ages, ages[1:], strict=False) if b > a]
    if not diffs:
        return 2.0
    diffs.sort()
    return diffs[len(diffs) // 2] or 2.0


def company_outage_metric(
    obs_by_round: dict[int, list[dict[str, Any]]],
    sites: list[dict[str, Any]],
    threshold: float = WIND_CLOSE_THRESHOLD,
) -> float:
    """Site-weighted outage metric (weighted closed-seconds), summed across rounds.

    ``obs_by_round`` maps round -> list of storm observations. ``sites`` is the list
    of company sites (each with x, y, weight). The metric accumulates, for each site,
    ``weight * closed_observations * observation_spacing`` over every round.
    """
    total = 0.0
    for obs_list in obs_by_round.values():
        if not obs_list:
            continue
        dt = _obs_spacing(obs_list)
        for site in sites:
            sx, sy, w = site["x"], site["y"], site["weight"]
            closed = sum(1 for o in obs_list if wind_at(sx, sy, o) >= threshold)
            total += w * closed * dt
    return total


def group_storm_obs(feed: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Group ``storm`` observations by round from a feed (or feed slice)."""
    out: dict[int, list[dict[str, Any]]] = {}
    for m in feed:
        if m.get("type") == "storm":
            out.setdefault(m["round"], []).append(m)
    return out
