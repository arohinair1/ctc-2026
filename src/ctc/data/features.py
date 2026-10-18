"""Build per-season feature tables used to fit and evaluate the valuation models.

Two kinds of features are distinguished:

* **Ground truth** (``source="manifest"``): the exact cumulative damage and season
  losses from the manifest final reports. Used to *fit* valuation models.
* **Reconstructed** (outage): site-weighted outage metrics computed from the public
  storm observations via :mod:`ctc.models.wind`.

Everything here is a pure function of the historical data; there is no look-ahead
because each row is one completed season.
"""

from __future__ import annotations

import pandas as pd

from ctc.config import ISLAND_BUSINESSES, company_sites
from ctc.data.history import History, Season
from ctc.models.wind import company_outage_metric, group_storm_obs


def season_outages(season: Season) -> dict[str, float]:
    """Site-weighted outage metric for every island business over the whole season."""
    obs_by_round = group_storm_obs(season.feed)
    sites = company_sites()
    return {sym: company_outage_metric(obs_by_round, sites[sym]) for sym in ISLAND_BUSINESSES}


def season_cumulative_damage(season: Season) -> dict[str, float]:
    """Cumulative final-report damage percent per island business over the season."""
    cum = {sym: 0.0 for sym in ISLAND_BUSINESSES}
    for storm in season.storms:
        for sym in ISLAND_BUSINESSES:
            cum[sym] += storm["damage_pct"][sym]
    return cum


def build_feature_table(history: History, n_seasons: int | None = None) -> pd.DataFrame:
    """One row per season with damage, outage, loss and settlement columns.

    Columns
    -------
    season, season_loss, settle_<SYM> for all 12 symbols,
    cumdmg_<SYM> and outage_<SYM> for the 5 island businesses,
    total_damage (sum of island-business cumulative damage),
    recon_damage (PORT+GRID+FERRY cumulative damage),
    grid_outage (GRID outage metric).
    """
    seasons = history.seasons if n_seasons is None else history.seasons[:n_seasons]
    rows = []
    for s in seasons:
        cum = season_cumulative_damage(s)
        out = season_outages(s)
        row: dict[str, float] = {"season": s.number, "season_loss": s.season_loss_musd}
        for sym, v in s.settlement.items():
            row[f"settle_{sym}"] = v
        for sym in ISLAND_BUSINESSES:
            row[f"cumdmg_{sym}"] = cum[sym]
            row[f"outage_{sym}"] = out[sym]
        row["total_damage"] = sum(cum.values())
        row["recon_damage"] = cum["PORT"] + cum["GRID"] + cum["FERRY"]
        row["recon_outage"] = out["PORT"] + out["GRID"] + out["FERRY"]
        row["grid_outage"] = out["GRID"]
        rows.append(row)
    return pd.DataFrame(rows)
