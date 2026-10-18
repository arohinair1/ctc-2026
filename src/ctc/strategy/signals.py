"""Baseline trading strategy: trade expected mispricing vs executable prices.

The strategy is deliberately simple and interpretable so teammates can extend it:

* ``edge = fair_value - mid`` for each symbol (fair value from the leakage-safe
  :class:`~ctc.forecasts.estimator.SeasonEstimator`);
* a trade is only worthwhile if the edge exceeds the executable cost (half the assumed
  spread) plus a safety cushion that reflects model uncertainty;
* position is scaled proportionally to the *excess* edge, up to the position limit and a
  per-step size cap (a crude liquidity model).

Nothing here assumes we can trade at the (non-executable) house consensus for free; the
spread and cushion are explicit, configurable execution assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass

from ctc.config import ALL_SYMBOLS
from ctc.risk.limits import clamp_order, position_limit


@dataclass
class StrategyConfig:
    half_spread: float = 0.0015  # fraction of mid charged on each side (execution assumption)
    cushion_frac: float = 0.001  # extra edge required, as a fraction of mid
    cushion_abs: float = 0.02  # extra edge required, absolute $
    uncertainty_k: float = 1.5  # required edge also exceeds k * fair-value uncertainty (sigma)
    edge_scale_frac: float = 0.02  # excess edge (as frac of mid) that maps to full-limit position
    max_trade_per_step_frac: float = 0.34  # per-step order cap as fraction of limit (liquidity model)
    symbols: tuple[str, ...] = tuple(ALL_SYMBOLS)


@dataclass
class Decision:
    symbol: str
    target: int
    order: int
    edge: float
    executable: float  # price we would transact at (ask if buying, bid if selling)


def executable_prices(mid: float, cfg: StrategyConfig) -> tuple[float, float]:
    """(bid, ask) implied by the assumed half-spread around the house mid."""
    return mid * (1.0 - cfg.half_spread), mid * (1.0 + cfg.half_spread)


def decide(
    fair: dict[str, float],
    mids: dict[str, float],
    positions: dict[str, int],
    cfg: StrategyConfig,
    uncertainty: dict[str, float] | None = None,
) -> list[Decision]:
    """Return one :class:`Decision` per tradable symbol with valid mid and fair value.

    If ``uncertainty`` (per-symbol 1-sigma on the fair value) is given, a trade is only
    taken when the edge also exceeds ``uncertainty_k`` sigmas; this gates trading to
    moments when we genuinely know more than the house (e.g. just after a damage report).
    """
    uncertainty = uncertainty or {}
    decisions: list[Decision] = []
    for sym in cfg.symbols:
        mid = mids.get(sym)
        fv = fair.get(sym)
        if mid is None or fv is None or mid <= 0:
            continue
        bid, ask = executable_prices(mid, cfg)
        required = (
            cfg.cushion_abs
            + cfg.cushion_frac * mid
            + cfg.half_spread * mid
            + cfg.uncertainty_k * uncertainty.get(sym, 0.0)
        )
        pos = positions.get(sym, 0)
        lim = position_limit(sym)
        scale = cfg.edge_scale_frac * mid
        edge_long = fv - ask  # buying at ask
        edge_short = bid - fv  # selling at bid
        if edge_long > required:
            frac = min(1.0, (edge_long - required) / max(scale, 1e-9))
            target = int(round(frac * lim))
            exec_px = ask
        elif edge_short > required:
            frac = min(1.0, (edge_short - required) / max(scale, 1e-9))
            target = -int(round(frac * lim))
            exec_px = bid
        else:
            target = 0
            exec_px = mid
        max_trade = max(1, int(cfg.max_trade_per_step_frac * lim))
        order = clamp_order(sym, pos, target, max_trade=max_trade)
        # the executable price depends on the actual order direction
        if order > 0:
            exec_px = ask
        elif order < 0:
            exec_px = bid
        decisions.append(Decision(sym, target, order, fv - mid, exec_px))
    return decisions
