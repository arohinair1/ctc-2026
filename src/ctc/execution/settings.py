"""Live-bot configuration and secure credential loading.

The bot key is read from the ``CTC_API_KEY`` environment variable, or from a ``.env``
file at the repository root (never committed; see ``.gitignore``). The key is treated as
a secret: it is never logged, printed or serialized. :meth:`LiveConfig.redacted` returns
a safe-to-log view.

Safety defaults:

* ``dry_run`` defaults to **True** — the bot computes and logs decisions but never submits
  state-changing requests (orders, cancels, ETF create/redeem). Live execution requires an
  explicit ``CTC_DRY_RUN=false``.
* Risk defaults are conservative and configurable via environment variables.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from ctc.config import REPO_ROOT

DEFAULT_HTTP_URL = "https://cornelltradingcompetition.org"
USER_AGENT = "ctc-2026-bot/0.1 (+https://github.com/arohinair1/ctc-2026)"


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Minimal ``.env`` loader (no third-party dependency).

    Parses ``KEY=VALUE`` lines, ignoring blanks and ``#`` comments, stripping optional
    surrounding single/double quotes. Values are injected into ``os.environ`` unless they
    are already set (``override=False``). Returns the parsed mapping.
    """
    p = Path(path) if path is not None else REPO_ROOT / ".env"
    parsed: dict[str, str] = {}
    if not p.exists():
        return parsed
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        parsed[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return parsed


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


@dataclass
class LiveConfig:
    """Resolved live-trading configuration. Build with :meth:`from_env`."""

    api_key: str = ""  # secret; never logged
    http_url: str = DEFAULT_HTTP_URL
    user_agent: str = USER_AGENT

    # Safety
    dry_run: bool = True
    kill_switch: bool = False  # when True, no new orders are ever proposed

    # Risk limits (see ctc.execution.risk.RiskManager)
    max_order_size_frac: float = 0.25  # per-order cap, fraction of the symbol position limit
    max_gross_exposure: float = 2_000_000.0  # max |notional| across the book ($)
    max_orders_per_min: int = 50  # below the exchange's 60/bot to keep headroom
    min_reprice_edge: float = 0.02  # don't resubmit unless the target moved by this ($)
    stale_after_s: float = 10.0  # reject trading on quotes older than this (seconds)
    max_session_loss: float = 10_000.0
    max_initial_gross_exposure: float = 10_000.0
    initial_max_order_size_frac: float = 0.01
    initial_orders_per_min: int = 5
    initial_max_order_size_units: int = 1
    automatic_transition_enabled: bool = False
    accounting_provider: str = ""
    order_registry_path: str = "research/runtime/order_registry.json"
    session_pnl_path: str = "research/runtime/session_pnl.json"
    process_lock_path: str = "research/runtime/bot.lock"

    def __post_init__(self) -> None:
        positive = {
            "max_order_size_frac": self.max_order_size_frac,
            "max_gross_exposure": self.max_gross_exposure,
            "max_session_loss": self.max_session_loss,
            "max_initial_gross_exposure": self.max_initial_gross_exposure,
            "initial_max_order_size_frac": self.initial_max_order_size_frac,
            "stale_after_s": self.stale_after_s,
        }
        if any(not math.isfinite(value) or value <= 0 for value in positive.values()):
            raise ValueError("risk caps and staleness limits must be positive finite numbers")
        if self.max_orders_per_min < 1 or self.initial_orders_per_min < 1:
            raise ValueError("order action limits must be at least one per minute")
        if self.initial_max_order_size_units < 1:
            raise ValueError("initial order units must be at least one")
        # These remain absolute ceilings even if an environment override is too large.
        self.max_order_size_frac = min(self.max_order_size_frac, 0.25)
        self.max_gross_exposure = min(self.max_gross_exposure, 2_000_000.0)
        self.max_orders_per_min = min(self.max_orders_per_min, 50)
        self.max_initial_gross_exposure = min(self.max_initial_gross_exposure, self.max_gross_exposure)
        self.initial_max_order_size_frac = min(self.initial_max_order_size_frac, self.max_order_size_frac)
        self.initial_orders_per_min = min(self.initial_orders_per_min, self.max_orders_per_min)

    # Connection
    ws_max_backoff_s: float = 30.0
    request_timeout_s: float = 10.0

    @property
    def ws_url(self) -> str:
        base = self.http_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return base.rstrip("/") + "/ws/v1/market-data"

    @classmethod
    def from_env(cls, *, load_env_file: bool = True) -> LiveConfig:
        if load_env_file:
            load_dotenv()
        return cls(
            api_key=os.environ.get("CTC_API_KEY", ""),
            http_url=os.environ.get("CTC_HTTP_URL", DEFAULT_HTTP_URL).rstrip("/"),
            user_agent=os.environ.get("CTC_USER_AGENT", USER_AGENT),
            dry_run=_env_bool("CTC_DRY_RUN", True),
            kill_switch=_env_bool("CTC_KILL_SWITCH", False),
            max_order_size_frac=_env_float("CTC_MAX_ORDER_SIZE_FRAC", 0.25),
            max_gross_exposure=_env_float("CTC_MAX_GROSS_EXPOSURE", 2_000_000.0),
            max_orders_per_min=_env_int("CTC_MAX_ORDERS_PER_MIN", 50),
            min_reprice_edge=_env_float("CTC_MIN_REPRICE_EDGE", 0.02),
            stale_after_s=_env_float("CTC_STALE_AFTER_S", 10.0),
            max_session_loss=_env_float("CTC_MAX_SESSION_LOSS", 10_000.0),
            max_initial_gross_exposure=_env_float("CTC_INITIAL_MAX_GROSS_EXPOSURE", 10_000.0),
            initial_max_order_size_frac=_env_float("CTC_INITIAL_MAX_ORDER_SIZE_FRAC", 0.01),
            initial_orders_per_min=_env_int("CTC_INITIAL_ORDERS_PER_MIN", 5),
            initial_max_order_size_units=_env_int("CTC_INITIAL_MAX_ORDER_UNITS", 1),
            automatic_transition_enabled=_env_bool("CTC_AUTO_TRANSITION", False),
            accounting_provider=os.environ.get("CTC_ACCOUNTING_PROVIDER", "").strip(),
            order_registry_path=os.environ.get(
                "CTC_ORDER_REGISTRY_PATH", "research/runtime/order_registry.json"
            ),
            session_pnl_path=os.environ.get("CTC_SESSION_PNL_PATH", "research/runtime/session_pnl.json"),
            process_lock_path=os.environ.get("CTC_PROCESS_LOCK_PATH", "research/runtime/bot.lock"),
        )

    def has_key(self) -> bool:
        return bool(self.api_key)

    def redacted(self) -> dict[str, object]:
        """A safe-to-log view with the key masked."""
        key = self.api_key
        masked = "<set>" if key else "<missing>"
        return {
            "http_url": self.http_url,
            "ws_url": self.ws_url,
            "api_key": masked,
            "dry_run": self.dry_run,
            "kill_switch": self.kill_switch,
            "max_order_size_frac": self.max_order_size_frac,
            "max_gross_exposure": self.max_gross_exposure,
            "max_orders_per_min": self.max_orders_per_min,
            "stale_after_s": self.stale_after_s,
            "max_session_loss": self.max_session_loss,
            "max_initial_gross_exposure": self.max_initial_gross_exposure,
            "initial_max_order_size_frac": self.initial_max_order_size_frac,
            "initial_orders_per_min": self.initial_orders_per_min,
            "initial_max_order_size_units": self.initial_max_order_size_units,
            "automatic_transition_enabled": self.automatic_transition_enabled,
            "process_lock_path": self.process_lock_path,
        }

    # never leak the key via repr
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"LiveConfig({self.redacted()})"


@dataclass
class _Secret:
    """Wrapper that keeps a secret out of logs/reprs (used internally)."""

    value: str = field(repr=False, default="")

    def __str__(self) -> str:  # pragma: no cover
        return "<redacted>"
