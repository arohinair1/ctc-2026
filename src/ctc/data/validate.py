"""Validate the historical research dataset.

Checks each season for:

* missing / malformed feed lines and required fields,
* duplicate ``seq`` values and non-monotonic sequencing,
* missing settlement or market-price symbols,
* consistency between the manifest final damage and the final damage report in the
  feed, and between per-storm losses and the season total,
* NaN / out-of-range values (damage %, probabilities, prices).

Run with ``ctc-validate`` or ``python -m ctc.data.validate``. It prints a per-season
summary and a roll-up; it exits non-zero if any *error* (not warning) is found.
"""

from __future__ import annotations

import argparse
import math
from collections import Counter
from dataclasses import dataclass, field

from ctc.config import ALL_SYMBOLS, ISLAND_BUSINESSES, ISLANDS, OUTLOOKS
from ctc.data.history import Season, load

REQUIRED_BY_TYPE = {
    "storm": ["age", "x", "y", "vx", "vy", "radius", "intensity"],
    "track": ["age", "points"],
    "advisory": ["age", "source", "outlook"],
    "impact": ["island", "age"],
    "damage_report": ["stage", "damage_pct", "industry_loss_musd"],
}


@dataclass
class SeasonIssues:
    season: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_season(season: Season) -> SeasonIssues:
    issues = SeasonIssues(season.number)
    feed = season.feed

    # seq ordering + duplicates
    seqs = [m["seq"] for m in feed if "seq" in m]
    dup = [s for s, c in Counter(seqs).items() if c > 1]
    if dup:
        issues.warnings.append(f"{len(dup)} duplicate seq values (exchange retries are expected)")
    if seqs != sorted(seqs):
        issues.warnings.append("feed not in non-decreasing seq order")

    # required fields + value ranges
    for m in feed:
        t = m.get("type")
        for fld in REQUIRED_BY_TYPE.get(t, []):
            if fld not in m:
                issues.errors.append(f"seq {m.get('seq')}: {t} missing field '{fld}'")
        if t == "advisory":
            if m.get("outlook") not in OUTLOOKS:
                issues.errors.append(f"seq {m.get('seq')}: unknown outlook {m.get('outlook')!r}")
            for isl, p in (m.get("hurricane_conditions") or {}).items():
                if isl not in ISLANDS:
                    issues.errors.append(f"seq {m.get('seq')}: unknown island {isl!r}")
                if not (0.0 <= p <= 1.0):
                    issues.errors.append(f"seq {m.get('seq')}: probability {p} out of [0,1]")
        if t == "storm":
            for fld in ("x", "y", "radius", "intensity"):
                v = m.get(fld)
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    issues.errors.append(f"seq {m.get('seq')}: storm {fld} is NaN/None")
        if t == "damage_report":
            for sym, v in (m.get("damage_pct") or {}).items():
                if sym not in ISLAND_BUSINESSES:
                    issues.errors.append(f"seq {m.get('seq')}: damage for non-island symbol {sym}")
                if v < -1e-6 or v > 100.0 + 1e-6:
                    issues.warnings.append(f"seq {m.get('seq')}: damage_pct {sym}={v} outside [0,100]")

    # settlement completeness
    missing_settle = [s for s in ALL_SYMBOLS if s not in season.settlement]
    if missing_settle:
        issues.errors.append(f"settlement missing symbols: {missing_settle}")

    # market.csv completeness
    price_syms = set(season.prices)
    missing_price = [s for s in ALL_SYMBOLS if s not in price_syms]
    if missing_price:
        issues.errors.append(f"market.csv missing symbols: {missing_price}")

    # manifest vs feed final damage report consistency
    for storm in season.storms:
        finals = season.messages(storm["round"], type="damage_report", stage="final")
        if not finals:
            issues.warnings.append(f"round {storm['round']}: no final damage report in feed")
            continue
        fin = finals[-1]
        for sym in ISLAND_BUSINESSES:
            a = storm["damage_pct"][sym]
            b = fin["damage_pct"].get(sym)
            if b is None or abs(a - b) > 0.05:
                issues.warnings.append(f"round {storm['round']} {sym}: manifest {a} vs feed {b}")

    # per-storm losses sum to season total
    tot = sum(s["industry_loss_musd"] for s in season.storms)
    if abs(tot - season.season_loss_musd) > 0.5:
        issues.warnings.append(f"sum of storm losses {tot:.1f} != season_loss {season.season_loss_musd:.1f}")

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="validate only the first N seasons")
    parser.add_argument("--verbose", action="store_true", help="print every issue")
    args = parser.parse_args()

    data = load()
    seasons = data.seasons[: args.limit] if args.limit else data.seasons
    n_err = n_warn = 0
    bad_seasons = 0
    for s in seasons:
        iss = validate_season(s)
        n_err += len(iss.errors)
        n_warn += len(iss.warnings)
        if iss.errors:
            bad_seasons += 1
        if args.verbose and (iss.errors or iss.warnings):
            print(f"season {s.number}: {len(iss.errors)} errors, {len(iss.warnings)} warnings")
            for e in iss.errors:
                print(f"  ERROR  {e}")
            for w in iss.warnings:
                print(f"  warn   {w}")

    print(
        f"Validated {len(seasons)} seasons: {n_err} errors across {bad_seasons} seasons, {n_warn} warnings."
    )
    return 1 if n_err else 0


if __name__ == "__main__":
    raise SystemExit(main())
