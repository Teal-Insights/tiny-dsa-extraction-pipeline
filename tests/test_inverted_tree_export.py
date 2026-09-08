"""Synthetic inverted-tree export shape."""

from __future__ import annotations

import inspect

from excel_grapher.exporter import CodeGenerator


def test_generate_modules_is_keyword_only_inverted_tree() -> None:
    params = inspect.signature(CodeGenerator.generate_modules).parameters
    assert "paradigm" not in params
    assert "targets" not in params
    assert list(params) == [
        "self",
        "series_bindings",
        "bindings_workbook",
        "blank_ranges",
    ]
    assert params["series_bindings"].kind is inspect.Parameter.KEYWORD_ONLY


def test_synthetic_inverted_tree_export_omits_ctx_helpers(
    synthetic_configured_pipeline,
) -> None:
    pipeline = synthetic_configured_pipeline
    modules = CodeGenerator(pipeline.graph).generate_modules(
        series_bindings=pipeline.series_bindings,
        bindings_workbook=pipeline.config.workbook_path,
    )
    assert "api.py" in modules
    assert "internals.py" in modules
    assert "runtime.py" in modules
    assert "data.py" in modules
    assert "_api_helpers.py" not in modules
    assert "_readers.py" not in modules
    assert "def make_context" not in modules["api.py"]
    assert "def set_input_rate" not in modules["api.py"]
    assert "def compute_result_a" in modules["api.py"]
    assert "def compute_result_b" in modules["api.py"]
