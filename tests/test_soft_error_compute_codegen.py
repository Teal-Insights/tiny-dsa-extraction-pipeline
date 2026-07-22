"""Soft-error rewrite for generated compute_* measure evaluation."""

from __future__ import annotations

from src.soft_error_compute_codegen import (
    ensure_xl_error_exception_import,
    rewrite_compute_measure_assignment,
)


def test_rewrite_wraps_xl_cell_measure_assignment_in_try_except() -> None:
    source_lines = [
        "def compute_example_series(ctx=None, *, inputs=None) -> Records:",
        "    measure_field = 'OBS_VALUE'",
        "    records: Records = []",
        "    for address, static_record in _OUTPUT_LEAVES_EXAMPLE_SERIES:",
        "        record = dict(static_record)",
        "        record[measure_field] = xl_cell(ctx, address)",
        "        records.append(record)",
        "    return records",
        "",
    ]
    rewritten = rewrite_compute_measure_assignment(source_lines)
    joined = "\n".join(rewritten)
    assert "record[measure_field] = xl_cell(ctx, address)" in joined
    assert "try:" in joined
    assert "except XlErrorException as _xl_err:" in joined
    assert "record[measure_field] = _xl_err.code" in joined


def test_ensure_import_adds_xl_error_exception_to_runtime_import() -> None:
    source = (
        "from .runtime import EvalContext, coerce_inputs_dict, xl_cell, xl_range_rows\n"
        "\n"
        "def make_context():\n"
        "    ...\n"
    )
    updated = ensure_xl_error_exception_import(source)
    assert (
        "from .runtime import EvalContext, XlErrorException, coerce_inputs_dict, "
        "xl_cell, xl_range_rows"
    ) in updated


def test_ensure_import_handles_parenthesized_multiline_runtime_import() -> None:
    source = (
        "from .runtime import (\n"
        "    EvalContext,\n"
        "    coerce_inputs_dict,\n"
        "    xl_cell,\n"
        "    xl_range_rows,\n"
        ")\n"
        "\n"
        "def make_context():\n"
        "    ...\n"
    )
    updated = ensure_xl_error_exception_import(source)
    assert "from .runtime import (, XlErrorException" not in updated
    assert (
        "from .runtime import (\n"
        "    EvalContext,\n"
        "    XlErrorException,\n"
        "    coerce_inputs_dict,\n"
        "    xl_cell,\n"
        "    xl_range_rows,\n"
        ")\n"
    ) in updated


def test_ensure_import_is_idempotent() -> None:
    source = "from .runtime import EvalContext, XlErrorException, xl_cell\n"
    assert ensure_xl_error_exception_import(source) == source


def test_ensure_import_is_idempotent_for_parenthesized_import() -> None:
    source = (
        "from .runtime import (\n"
        "    EvalContext,\n"
        "    XlErrorException,\n"
        "    xl_cell,\n"
        ")\n"
    )
    assert ensure_xl_error_exception_import(source) == source


def test_ensure_import_repairs_mangled_parenthesized_runtime_import() -> None:
    source = (
        "from .runtime import (, XlErrorException\n"
        "    EvalContext,\n"
        "    XlErrorException,\n"
        "    coerce_inputs_dict,\n"
        "    xl_cell,\n"
        "    xl_range_rows,\n"
        ")\n"
    )
    updated = ensure_xl_error_exception_import(source)
    assert updated == (
        "from .runtime import (\n"
        "    EvalContext,\n"
        "    XlErrorException,\n"
        "    coerce_inputs_dict,\n"
        "    xl_cell,\n"
        "    xl_range_rows,\n"
        ")\n"
    )
