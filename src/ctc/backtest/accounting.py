"""Portfolio accounting for the backtester.

Tracks cash, positions, turnover and transaction costs, and computes PnL at
settlement. Execution prices already include the assumed spread (see
:mod:`ctc.strategy.signals`); the cost tracked here is the spread paid versus the
house mid, so it can be reported and stress-tested separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Portfolio:
    cash: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    n_trades: int = 0
    turnover: float = 0.0  # gross traded notional
    spread_cost: float = 0.0  # $ paid vs mid due to the assumed spread
    peak_equity: float = 0.0
    max_drawdown: float = 0.0

    def fill(self, symbol: str, qty: int, price: float, mid: float) -> None:
        """Apply a fill of ``qty`` (signed) at ``price``; ``mid`` is the house consensus."""
        if qty == 0:
            return
        self.cash -= qty * price
        self.positions[symbol] = self.positions.get(symbol, 0) + qty
        self.n_trades += 1
        self.turnover += abs(qty) * mid
        self.spread_cost += abs(qty) * abs(price - mid)

    def equity(self, marks: dict[str, float]) -> float:
        eq = self.cash
        for sym, pos in self.positions.items():
            if pos and sym in marks:
                eq += pos * marks[sym]
        return eq

    def update_drawdown(self, marks: dict[str, float]) -> None:
        eq = self.equity(marks)
        self.peak_equity = max(self.peak_equity, eq)
        self.max_drawdown = min(self.max_drawdown, eq - self.peak_equity)

    def settle(self, settlement: dict[str, float]) -> float:
        """Realized PnL at settlement = cash + positions marked at settlement values."""
        return self.equity(settlement)
