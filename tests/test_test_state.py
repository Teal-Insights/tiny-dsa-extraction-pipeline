"""Tests for reset_pipeline_test_state."""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from tests.fixtures.test_state import reset_pipeline_test_state

_DIFFERENTIAL_HARNESS_MODULE = "tests.differential.differential_test_exported_library"


def test_reset_pipeline_test_state_clears_probe_modules() -> None:
    sys.modules[_DIFFERENTIAL_HARNESS_MODULE] = types.ModuleType(
        _DIFFERENTIAL_HARNESS_MODULE
    )

    reset_pipeline_test_state()

    assert _DIFFERENTIAL_HARNESS_MODULE not in sys.modules


def test_pipeline_context_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.pipeline_context")
