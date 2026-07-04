"""Centralized logging configuration for pipeline entry points."""

from __future__ import annotations

import logging
import os

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def configure_logging() -> None:
    """Configure root and SDK loggers once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        force=True,
    )

    for logger_name in ("openai", "httpx"):
        logging.getLogger(logger_name).setLevel(level)

    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True
