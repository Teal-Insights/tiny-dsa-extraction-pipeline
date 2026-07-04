"""Tests for reset_pipeline_test_state."""

from __future__ import annotations

import sys
import types

import pytest

from src.pipeline_config import load_pipeline_config
from src.pipeline_context import activate_pipeline_config, require_pipeline_config
from tests.fixtures.test_state import reset_pipeline_test_state


def test_reset_pipeline_test_state_clears_active_config_and_probe_modules() -> None:
    activate_pipeline_config(load_pipeline_config())
    sys.modules["_runtime_symbols_probe"] = types.ModuleType("_runtime_symbols_probe")

    reset_pipeline_test_state()

    with pytest.raises(RuntimeError, match="Pipeline configuration is not active"):
        require_pipeline_config()
    assert "_runtime_symbols_probe" not in sys.modules
