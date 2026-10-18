"""Chronological, event-driven backtester.

Replays a season in timestamp order, interleaving the public feed and the house
consensus price samples. At each price sample it:

1. feeds all feed messages that arrived up to that time into a leakage-safe
   :class:`~ctc.forecasts.estimator.SeasonEstimator` (no look-ahead),
2. computes fair values and compares them to *executable* prices derived from the
   house mid plus an assumed spread,
3. places (simulated) trades subject to position limits and a per-step size cap,
4. marks the portfolio to the house mid for drawdown tracking.

At the close it settles every position at the season's settlement values.

Important execution caveat: the research data contains the house *consensus* price
only (no order book, depth or true spread). Fills are therefore simulated at
``mid * (1 +/- half_spread)``. This is an assumption, not observed execution; results
must be read as edge-capture under that assumption and validated on the testnet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ctc.backtest.accounting import Portfolio
from ctc.config import ALL_SYMBOLS
from ctc.data.history import Season
from ctc.forecasts.estimator import SeasonEstimator
from ctc.strategy.signals import StrategyConfig, decide


@dataclass
class SeasonResult:
    season: int
    pnl: float
    n_trades: int
    turnover: float
    spread_cost: float
    max_drawdown: float
    final_positions: dict[str, int]
    pnl_by_symbol: dict[str, float]


@dataclass
class Backtester:
    estimator: SeasonEstimator
    cfg: StrategyConfig = field(default_factory=StrategyConfig)

    def run_season(self, season: Season) -> SeasonResult:
        self.estimator.reset()
        pf = Portfolio()

        feed = sorted(season.feed, key=lambda m: (m.get("time", ""), m.get("seq", 0)))
        fi = 0
        times = season.times
        price_cols = season.prices
        settlement = season.settlement

        # per-symbol entry cost basis to attribute PnL by symbol at settlement
        cost_basis: dict[str, float] = dict.fromkeys(ALL_SYMBOLS, 0.0)

        for i, t in enumerate(times):
            # ingest all feed messages with time <= current sample time
            while fi < len(feed) and feed[fi].get("time", "") <= t:
                self.estimator.ingest(feed[fi])
                fi += 1

            mids = {s: price_cols[s][i] for s in ALL_SYMBOLS if s in price_cols}
            fair, unc = self.estimator.fair_values_with_uncertainty()
            for d in decide(fair, mids, pf.positions, self.cfg, uncertainty=unc):
                if d.order != 0:
                    pf.fill(d.symbol, d.order, d.executable, mids[d.symbol])
                    cost_basis[d.symbol] += d.order * d.executable
            pf.update_drawdown(mids)

        pnl = pf.settle(settlement)
        pnl_by_symbol = {s: pf.positions.get(s, 0) * settlement[s] - cost_basis[s] for s in ALL_SYMBOLS}
        return SeasonResult(
            season=season.number,
            pnl=pnl,
            n_trades=pf.n_trades,
            turnover=pf.turnover,
            spread_cost=pf.spread_cost,
            max_drawdown=pf.max_drawdown,
            final_positions=dict(pf.positions),
            pnl_by_symbol=pnl_by_symbol,
        )
