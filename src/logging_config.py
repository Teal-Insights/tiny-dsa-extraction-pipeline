"""Central logging configuration for the extraction pipeline entry points.

The pipeline spends most of its wall-clock time in blocking LLM calls, so
visibility into pipeline stage, validation retries, and the OpenAI SDK's own
rate-limit backoff is essential for telling a slow call apart from a hang. This
module configures the root logger from ``LOG_LEVEL`` and raises the third-party
HTTP/SDK loggers to the same level so their retry and request lines surface.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def configure_logging() -> None:
    """Configure root logging once from the ``LOG_LEVEL`` environment variable.

    Defaults to ``INFO``. The ``openai`` and ``httpx`` loggers are set to the
    same level so the SDK's built-in retry/backoff messages and per-request
    lines become visible; ``httpcore`` is pinned to ``WARNING`` to keep its
    byte-level chatter out of the log. Calling this more than once is a no-op.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("openai").setLevel(level)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))

    _CONFIGURED = True
