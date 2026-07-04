"""Environment variable parsing helpers."""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no"})


def env_flag(name: str, *, strict: bool = False) -> bool:
    """Return whether ``name`` is set to a recognized truthy value.

    When ``strict`` is False, any other non-empty value is treated as true.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in _FALSEY:
        return False
    if lowered in _TRUTHY:
        return True
    return not strict


def env_float(name: str) -> float | None:
    """Parse a float env var, returning ``None`` when unset or empty."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return float(raw)


def env_int(name: str, default: int) -> int:
    """Parse an integer env var, returning ``default`` when unset or empty."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)
