"""Exploratory data analysis over the 600 research seasons.

Produces quantitative summaries (and a few figures) covering storm patterns, damage
distributions, insured losses, settlement relationships and house-price behaviour.
Writes ``research/results/eda_summary.json`` and PNGs under
``research/results/figures/`` (both gitignored; reproducible via ``ctc-eda``).
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")

import numpy as np  # noqa: E402

from ctc.config import ISLAND_BUSINESSES, RESULTS_DIR, START_PRICES  # noqa: E402
from ctc.data.history import load  # noqa: E402


def reference_example(history) -> dict:
    right = total = 0
    for season in history.seasons:
        for storm in season.storms:
            flip = storm["turned_erratic_at"]
            for adv in season.messages(round=storm["round"], type="advisory", source="official"):
                truth = "erratic" if flip is not None and adv["age"] >= flip else storm["pattern"]
                right += adv["outlook"] == truth
                total += 1
    return {"accuracy": right / total, "right": right, "total": total}


def storm_stats(history) -> dict:
    patterns = Counter()
    erratic = 0
    n_storms = 0
    flip_ages = []
    by_round = defaultdict(Counter)
    for s in history.seasons:
        for storm in s.storms:
            patterns[storm["pattern"]] += 1
            by_round[storm["round"]][storm["pattern"]] += 1
            n_storms += 1
            if storm["turned_erratic_at"] is not None:
                erratic += 1
                flip_ages.append(storm["turned_erratic_at"])
    return {
        "n_storms": n_storms,
        "pattern_freq": {k: v / n_storms for k, v in patterns.most_common()},
        "erratic_turn_frac": erratic / n_storms,
        "erratic_turn_age_mean": float(np.mean(flip_ages)) if flip_ages else None,
        "pattern_freq_by_round": {r: dict(c) for r, c in by_round.items()},
    }


def damage_stats(history) -> dict:
    per_storm = defaultdict(list)
    per_pattern = defaultdict(lambda: defaultdict(list))
    losses = []
    season_losses = []
    for s in history.seasons:
        season_losses.append(s.season_loss_musd)
        for storm in s.storms:
            losses.append(storm["industry_loss_musd"])
            for sym in ISLAND_BUSINESSES:
                d = storm["damage_pct"][sym]
                per_storm[sym].append(d)
                per_pattern[storm["pattern"]][sym].append(d)

    def q(v):
        a = np.array(v)
        return {
            "mean": float(a.mean()),
            "std": float(a.std()),
            "p50": float(np.percentile(a, 50)),
            "p90": float(np.percentile(a, 90)),
            "max": float(a.max()),
            "frac_zero": float((a < 1e-9).mean()),
        }

    return {
        "per_company": {sym: q(per_storm[sym]) for sym in ISLAND_BUSINESSES},
        "per_pattern_mean_damage": {
            pat: {sym: float(np.mean(per_pattern[pat][sym])) for sym in ISLAND_BUSINESSES}
            for pat in per_pattern
        },
        "storm_loss": q(losses),
        "season_loss": q(season_losses),
    }


def settlement_vs_start(history) -> dict:
    out = {}
    for sym, start in START_PRICES.items():
        vals = np.array([s.settlement[sym] for s in history.seasons])
        out[sym] = {
            "start": start,
            "settle_mean": float(vals.mean()),
            "settle_std": float(vals.std()),
            "settle_min": float(vals.min()),
            "settle_max": float(vals.max()),
            "mean_minus_start": float(vals.mean() - start),
        }
    return out


def _figures(history, stats) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    figdir = RESULTS_DIR / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    paths = []

    # pattern frequency
    pf = stats["storms"]["pattern_freq"]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.bar(list(pf.keys()), list(pf.values()))
    ax.set_title("Storm pattern frequency (600 seasons)")
    ax.set_ylabel("fraction")
    fig.tight_layout()
    p = figdir / "pattern_freq.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(str(p))

    # season loss distribution
    losses = [s.season_loss_musd for s in history.seasons]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.hist(losses, bins=40)
    ax.set_title("Season insured-loss distribution ($M)")
    ax.set_xlabel("season_loss_musd")
    fig.tight_layout()
    p = figdir / "season_loss_hist.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(str(p))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    history = load()
    stats = {
        "n_seasons": len(history),
        "reference_example": reference_example(history),
        "storms": storm_stats(history),
        "damage": damage_stats(history),
        "settlement_vs_start": settlement_vs_start(history),
    }
    if not args.no_figures:
        stats["figures"] = _figures(history, stats)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "eda_summary.json").write_text(json.dumps(stats, indent=2))

    ref = stats["reference_example"]
    print(f"Reference example: official advisories named the true pattern {ref['accuracy']:.0%}")
    print(f"Storm patterns: {stats['storms']['pattern_freq']}")
    print(f"Erratic turn fraction: {stats['storms']['erratic_turn_frac']:.1%}")
    print(f"Season loss: mean ${stats['damage']['season_loss']['mean']:.0f}M "
          f"p90 ${stats['damage']['season_loss']['p90']:.0f}M "
          f"max ${stats['damage']['season_loss']['max']:.0f}M")
    print(f"Wrote {RESULTS_DIR / 'eda_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
