"""Tests for reset_pipeline_test_state."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

from src.runtime_symbols import (
    _allowed_runtime_symbols_cached,
    allowed_runtime_symbols,
)
from tests.fixtures.test_state import reset_pipeline_test_state


def test_reset_pipeline_test_state_clears_caches_and_probe_modules(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.py"
    runtime.write_text(
        "class XlError(Exception):\n    pass\n\ndef xl_eval():\n    return 1\n",
        encoding="utf-8",
    )
    allowed_runtime_symbols(tmp_path)
    assert _allowed_runtime_symbols_cached.cache_info().currsize >= 1
    sys.modules["_runtime_symbols_probe"] = types.ModuleType("_runtime_symbols_probe")

    reset_pipeline_test_state()

    assert _allowed_runtime_symbols_cached.cache_info().currsize == 0
    assert "_runtime_symbols_probe" not in sys.modules


def test_pipeline_context_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.pipeline_context")
