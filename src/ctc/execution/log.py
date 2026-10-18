"""Structured logging helpers with secret redaction.

All bot components log through :func:`get_logger`. Never pass the API key to a log call;
:func:`redact` is available for defensive masking of arbitrary strings.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(level: str | int | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = level or os.environ.get("CTC_LOG_LEVEL", "INFO")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S"))
    root = logging.getLogger("ctc")
    root.setLevel(lvl)
    root.handlers[:] = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"ctc.{name}")


def redact(text: str, secret: str | None) -> str:
    """Mask ``secret`` wherever it appears in ``text``."""
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text
