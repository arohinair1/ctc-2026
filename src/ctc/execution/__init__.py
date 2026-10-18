"""Live exchange integration: async client, state, risk and the trading loop.

Public surface:

* :class:`~ctc.execution.settings.LiveConfig` — configuration + secure key loading.
* :class:`~ctc.execution.client.ExchangeClient` — async REST + WebSocket client.
* :class:`~ctc.execution.state.LiveState` — market/season state + leakage-safe estimator.
* :class:`~ctc.execution.risk.RiskManager` — centralized pre-trade checks.
* :class:`~ctc.execution.trader.LiveTrader` — the state→valuation→strategy→risk→exec loop.

The bot defaults to dry-run; see docs/BOT_OPERATIONS.md and docs/RISK.md.
"""

from __future__ import annotations

from ctc.execution.client import ExchangeClient, ExchangeError, RateLimiter
from ctc.execution.risk import RiskDecision, RiskManager, gross_exposure
from ctc.execution.settings import LiveConfig
from ctc.execution.state import LiveState
from ctc.execution.trader import LiveTrader, ProposalLog

__all__ = [
    "LiveConfig",
    "ExchangeClient",
    "ExchangeError",
    "RateLimiter",
    "LiveState",
    "RiskManager",
    "RiskDecision",
    "gross_exposure",
    "LiveTrader",
    "ProposalLog",
]
