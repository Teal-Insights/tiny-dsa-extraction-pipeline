"""Synthetic inverted-tree export shape."""

from __future__ import annotations

from excel_grapher.exporter import CodeGenerator

from src.extraction_pipeline import call_generate_modules


def test_synthetic_inverted_tree_export_omits_ctx_helpers(
    synthetic_configured_pipeline,
) -> None:
    pipeline = synthetic_configured_pipeline
    modules = call_generate_modules(
        CodeGenerator(pipeline.graph),
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
