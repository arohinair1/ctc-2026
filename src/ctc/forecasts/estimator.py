"""Leakage-safe running estimate of each symbol's fundamental (settlement) value.

A :class:`SeasonEstimator` ingests feed messages in timestamp order and, at any
point, produces the best estimate of the *final* season outcome (cumulative damage,
site outages and total insured losses) using only information available so far:

* rounds whose **final** damage report has arrived -> exact values;
* a round with only a **preliminary** report -> the preliminary estimate;
* a round whose storm has been observed but not yet reported -> the track-based
  :class:`~ctc.forecasts.damage.DamageForecaster` (and observed outage so far);
* a round that has not started -> historical **priors**.

Those estimated outcomes are mapped to settlement values by the
:class:`~ctc.models.valuation.ValuationModel`. This is the single inference entry
point used by the backtester and (later) the live trading loop.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ctc.config import ISLAND_BUSINESSES, WIND_CLOSE_THRESHOLD, company_sites
from ctc.forecasts.damage import DamageForecaster
from ctc.models.valuation import ValuationModel
from ctc.models.wind import company_outage_metric

# Storm observations arrive every 2 real seconds (case.json timing.obs_every).
_OBS_DT = 2.0


@dataclass
class Priors:
    """Unconditional per-round means and variances for storms not yet observed, plus
    the error statistics of the preliminary damage report (prelim vs final)."""

    damage_by_round: dict[int, dict[str, float]]
    loss_by_round: dict[int, float]
    outage_by_round: dict[int, dict[str, float]]
    damage_var_by_round: dict[int, dict[str, float]]
    loss_var_by_round: dict[int, float]
    outage_var_by_round: dict[int, dict[str, float]]
    prelim_damage_err_var: dict[str, float]
    prelim_loss_err_var: float


def compute_priors(history, season_numbers: list[int]) -> Priors:
    want = set(season_numbers)
    dmg: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    loss: dict[int, list[float]] = defaultdict(list)
    outage: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    prelim_err: dict[str, list[float]] = defaultdict(list)
    prelim_loss_err: list[float] = []
    sites = company_sites()
    for season in history.seasons:
        if season.number not in want:
            continue
        obs_by_round = defaultdict(list)
        for m in season.feed:
            if m.get("type") == "storm":
                obs_by_round[m["round"]].append(m)
        for storm in season.storms:
            r = storm["round"]
            for sym in ISLAND_BUSINESSES:
                dmg[r][sym].append(storm["damage_pct"][sym])
                outage[r][sym].append(company_outage_metric({r: obs_by_round.get(r, [])}, sites[sym]))
            loss[r].append(storm["industry_loss_musd"])
            # preliminary vs final report error for this round
            prelims = season.messages(r, type="damage_report", stage="preliminary")
            finals = season.messages(r, type="damage_report", stage="final")
            if prelims and finals:
                p, f = prelims[-1], finals[-1]
                for sym in ISLAND_BUSINESSES:
                    prelim_err[sym].append(p["damage_pct"][sym] - f["damage_pct"][sym])
                prelim_loss_err.append(p["industry_loss_musd"] - f["industry_loss_musd"])

    def _mean(d):
        return {r: {s: float(np.mean(v)) for s, v in dd.items()} for r, dd in d.items()}

    def _var(d):
        return {r: {s: float(np.var(v)) for s, v in dd.items()} for r, dd in d.items()}

    return Priors(
        damage_by_round=_mean(dmg),
        loss_by_round={r: float(np.mean(v)) for r, v in loss.items()},
        outage_by_round=_mean(outage),
        damage_var_by_round=_var(dmg),
        loss_var_by_round={r: float(np.var(v)) for r, v in loss.items()},
        outage_var_by_round=_var(outage),
        prelim_damage_err_var={s: float(np.var(v)) for s, v in prelim_err.items()},
        prelim_loss_err_var=float(np.var(prelim_loss_err)) if prelim_loss_err else 0.0,
    )


@dataclass
class SeasonEstimator:
    valuation: ValuationModel
    forecaster: DamageForecaster | None
    priors: Priors
    use_forecaster: bool = True
    rounds: tuple[int, ...] = (1, 2, 3)

    # mutable state
    reported_damage: dict[str, float] = field(default_factory=dict)
    reported_loss: float = 0.0
    reported_outage: dict[str, float] = field(default_factory=dict)
    reported_rounds: set = field(default_factory=set)
    prelim: dict[int, dict[str, Any]] = field(default_factory=dict)
    current_round: int | None = None
    _sites: dict = field(default_factory=company_sites)
    # incremental per-round observed accumulators (updated on every storm obs)
    _obs_outage: dict = field(default_factory=dict)
    _exp2: dict = field(default_factory=dict)
    _peak: dict = field(default_factory=dict)
    _obs_started: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.reported_damage = dict.fromkeys(ISLAND_BUSINESSES, 0.0)
        self.reported_outage = dict.fromkeys(ISLAND_BUSINESSES, 0.0)
        self.reported_loss = 0.0
        self.reported_rounds = set()
        self.prelim = {}
        self.current_round = None
        self._obs_outage = defaultdict(lambda: dict.fromkeys(ISLAND_BUSINESSES, 0.0))
        self._exp2 = defaultdict(lambda: dict.fromkeys(ISLAND_BUSINESSES, 0.0))
        self._peak = defaultdict(lambda: dict.fromkeys(ISLAND_BUSINESSES, 0.0))
        self._obs_started = set()

    # ---- ingestion -----------------------------------------------------
    def _ingest_obs(self, msg: dict[str, Any]) -> None:
        r = msg["round"]
        self._obs_started.add(r)
        radius = msg["radius"]
        if radius <= 0:
            return
        inten = msg["intensity"]
        ox, oy = msg["x"], msg["y"]
        oo = self._obs_outage[r]
        e2 = self._exp2[r]
        pk = self._peak[r]
        for sym in ISLAND_BUSINESSES:
            acc_o = acc_e = acc_p = 0.0
            for site in self._sites[sym]:
                d = math.hypot(ox - site["x"], oy - site["y"])
                wind = inten * math.exp(-d / radius)
                w = site["weight"]
                if wind >= WIND_CLOSE_THRESHOLD:
                    acc_o += w * _OBS_DT
                acc_e += w * wind * wind * _OBS_DT
                acc_p = max(acc_p, w * wind)
            oo[sym] += acc_o
            e2[sym] += acc_e
            pk[sym] = max(pk[sym], acc_p)

    def ingest(self, msg: dict[str, Any]) -> None:
        r = msg.get("round")
        if isinstance(r, int):
            self.current_round = r
        t = msg.get("type")
        if t == "storm":
            self._ingest_obs(msg)
        elif t == "damage_report":
            rr = msg["round"]
            if msg.get("stage") == "final":
                for sym in ISLAND_BUSINESSES:
                    self.reported_damage[sym] += msg["damage_pct"][sym]
                    self.reported_outage[sym] += self._obs_outage[rr][sym]
                self.reported_loss += msg["industry_loss_musd"]
                self.reported_rounds.add(rr)
                self.prelim.pop(rr, None)
            else:  # preliminary
                self.prelim[rr] = {
                    "damage": {sym: msg["damage_pct"][sym] for sym in ISLAND_BUSINESSES},
                    "loss": msg["industry_loss_musd"],
                }

    # ---- estimation ----------------------------------------------------
    def _estimate(self) -> tuple[dict[str, float], dict[str, float]]:
        """Return (features, feature_variances).

        feature_variances has keys ``cumdmg_<SYM>``, ``outage_<SYM>``, ``season_loss``.
        """
        cum = dict(self.reported_damage)
        out = dict(self.reported_outage)
        loss = self.reported_loss
        dvar = dict.fromkeys(ISLAND_BUSINESSES, 0.0)
        ovar = dict.fromkeys(ISLAND_BUSINESSES, 0.0)
        lvar = 0.0
        for r in self.rounds:
            if r in self.reported_rounds:
                continue
            started = r in self._obs_started
            if r in self.prelim:
                pr = self.prelim[r]
                for sym in ISLAND_BUSINESSES:
                    cum[sym] += pr["damage"][sym]
                    out[sym] += self._obs_outage[r][sym]
                    dvar[sym] += self.priors.prelim_damage_err_var.get(sym, 0.0)
                loss += pr["loss"]
                lvar += self.priors.prelim_loss_err_var
            elif started and self.use_forecaster and self.forecaster is not None:
                feats = {sym: (self._exp2[r][sym], self._peak[r][sym]) for sym in ISLAND_BUSINESSES}
                dmg = self.forecaster.predict_damage(feats)
                for sym in ISLAND_BUSINESSES:
                    cum[sym] += dmg[sym]
                    out[sym] += self._obs_outage[r][sym]
                    dvar[sym] += self.forecaster.resid_std.get(sym, 0.0) ** 2
                loss += self.forecaster.predict_loss(dmg)
                lvar += self.forecaster.loss_resid_std**2
            else:
                for sym in ISLAND_BUSINESSES:
                    cum[sym] += self.priors.damage_by_round.get(r, {}).get(sym, 0.0)
                    out[sym] += self.priors.outage_by_round.get(r, {}).get(sym, 0.0)
                    dvar[sym] += self.priors.damage_var_by_round.get(r, {}).get(sym, 0.0)
                    ovar[sym] += self.priors.outage_var_by_round.get(r, {}).get(sym, 0.0)
                loss += self.priors.loss_by_round.get(r, 0.0)
                lvar += self.priors.loss_var_by_round.get(r, 0.0)

        features = {"season_loss": loss}
        variances = {"season_loss": lvar}
        for sym in ISLAND_BUSINESSES:
            features[f"cumdmg_{sym}"] = cum[sym]
            features[f"outage_{sym}"] = out[sym]
            variances[f"cumdmg_{sym}"] = dvar[sym]
            variances[f"outage_{sym}"] = ovar[sym]
        return features, variances

    def estimate_final_features(self) -> dict[str, float]:
        return self._estimate()[0]

    def fair_values(self) -> dict[str, float]:
        return self.valuation.predict_all(self.estimate_final_features())

    def fair_values_with_uncertainty(self) -> tuple[dict[str, float], dict[str, float]]:
        """Fair value and a 1-sigma uncertainty per symbol propagated from the inputs."""
        features, var = self._estimate()
        fair = self.valuation.predict_all(features)
        std = self._propagate_uncertainty(features, var)
        return fair, std

    def _propagate_uncertainty(self, features: dict[str, float], var: dict[str, float]) -> dict[str, float]:
        vm = self.valuation
        std: dict[str, float] = {}
        # island businesses: linear in damage and outage
        for sym, m in vm.islands.items():
            v = (m.b_damage**2) * var[f"cumdmg_{sym}"] + (m.c_outage**2) * var[f"outage_{sym}"]
            std[sym] = math.sqrt(max(v, 0.0))
        # insurers: slope * loss, only where the loss estimate overlaps the layer
        loss_est = features["season_loss"]
        loss_sd = math.sqrt(max(var["season_loss"], 0.0))
        for sym, m in vm.insurers.items():
            lo, hi = loss_est - loss_sd, loss_est + loss_sd
            overlap = max(0.0, min(hi, m.attach + m.width) - max(lo, m.attach))
            if loss_sd > 0:
                frac = overlap / (2 * loss_sd)
            else:
                frac = 1.0 if m.attach <= loss_est <= m.attach + m.width else 0.0
            std[sym] = abs(m.slope) * loss_sd * frac
        # rebuilders: sqrt drivers -> delta method
        for sym, m in vm.rebuilders.items():
            v = 0.0
            from ctc.models.valuation import _derive

            dfeat = _derive(features)
            for d, c in zip(m.drivers, m.coefs, strict=False):
                driver = max(dfeat.get(d, 0.0), 1e-6)
                dvar = self._driver_variance(d, var)
                deriv = c / (2.0 * math.sqrt(driver))
                v += (deriv**2) * dvar
            std[sym] = math.sqrt(max(v, 0.0))
        # add each symbol's irreducible valuation residual as an uncertainty floor
        for sym in list(std):
            rs = vm.resid_std.get(sym, 0.0)
            std[sym] = math.sqrt(std[sym] ** 2 + rs**2)
        # ETFs: conservative linear sum of constituent uncertainties, plus ETF residual floor
        from ctc.config import baskets

        for etf, basket in baskets().items():
            base = sum(abs(q) * std.get(s, 0.0) for s, q in basket.items())
            std[etf] = math.sqrt(base**2 + vm.resid_std.get(etf, 0.0) ** 2)
        return std

    @staticmethod
    def _driver_variance(driver: str, var: dict[str, float]) -> float:
        if driver == "season_loss":
            return var["season_loss"]
        if driver == "grid_outage":
            return var["outage_GRID"]
        if driver == "recon_damage":
            return sum(var[f"cumdmg_{s}"] for s in ("PORT", "GRID", "FERRY"))
        if driver == "recon_outage":
            return sum(var[f"outage_{s}"] for s in ("PORT", "GRID", "FERRY"))
        return 0.0
