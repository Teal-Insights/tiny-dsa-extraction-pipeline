from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

PhaseBPlugin: TypeAlias = Callable[[str], str]


def apply_phase_b_plugins(source: str, plugins: tuple[PhaseBPlugin, ...]) -> str:
    updated = source
    for plugin in plugins:
        updated = plugin(updated)
    return updated
