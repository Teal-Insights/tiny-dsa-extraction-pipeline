"""Tests for the startup-site input/output catalog CLI."""

from __future__ import annotations

import csv
import importlib
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from excel_grapher.core.cell_types import Between, RealBetween
from excel_grapher.series_bindings import derive_input_series, derive_output_series

from tests.conftest import SyntheticConfiguredPipeline
from tests.fixtures.synthetic_pipeline import (
    synthetic_pipeline_config,
    write_synthetic_workbook,
)


def test_format_constraint_literal_enum() -> None:
    from scripts.i_o_tables import format_constraint

    formatted = format_constraint(Literal["Borvelia", "Litellia", "Aurelium"])
    assert formatted.dtype == "string"
    assert formatted.acceptable_values == "Borvelia, Litellia, Aurelium"


def test_format_constraint_singleton_literal() -> None:
    from scripts.i_o_tables import format_constraint

    formatted = format_constraint(Literal[0])
    assert formatted.dtype == "int"
    assert formatted.acceptable_values == "0"


def test_format_constraint_between() -> None:
    from scripts.i_o_tables import format_constraint

    formatted = format_constraint(Annotated[int, Between(1, 5)])
    assert formatted.dtype == "int"
    assert formatted.acceptable_values == "1 ≤ x ≤ 5"


def test_format_constraint_real_between() -> None:
    from scripts.i_o_tables import format_constraint

    formatted = format_constraint(Annotated[float, RealBetween(0.0, 100.0)])
    assert formatted.dtype == "float"
    assert formatted.acceptable_values == "0.0 ≤ x ≤ 100.0"


def test_format_constraint_falls_back_to_measure_dtype() -> None:
    from scripts.i_o_tables import format_constraint

    formatted = format_constraint(None, fallback_dtype="float")
    assert formatted.dtype == "float"
    assert formatted.acceptable_values == ""


def test_format_constraint_open_ended_interval() -> None:
    from scripts.i_o_tables import format_constraint

    formatted = format_constraint(Annotated[int, Between(None, 10)])
    assert formatted.dtype == "int"
    assert formatted.acceptable_values == "x ≤ 10"


def test_effective_domain_annotations_overlay_wins(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from scripts.i_o_tables import effective_domain_annotations

    overlay = {"Inputs!A1": Annotated[float, RealBetween(1.0, 2.0)]}
    config = replace(synthetic_configured_pipeline.config, constraints=overlay)
    annotations = effective_domain_annotations(
        config, bindings=synthetic_configured_pipeline.series_bindings
    )
    assert annotations["Inputs!A1"] is overlay["Inputs!A1"]


def test_synthetic_input_catalog_rows(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from scripts.i_o_tables import (
        effective_domain_annotations,
        input_catalog_rows,
    )

    config = synthetic_configured_pipeline.config
    graph = synthetic_configured_pipeline.graph
    bindings = synthetic_configured_pipeline.series_bindings
    rows = input_catalog_rows(
        derive_input_series(graph, bindings, workbook=config.workbook_path),
        effective_domain_annotations(config, bindings=bindings),
        bindings,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["series_id"] == "input_rate"
    assert row["address"] == "Inputs!A1"
    assert row["dtype"] == "float"
    assert row["acceptable_values"] == "0.0 ≤ x ≤ 100.0"
    assert row["default"] == "10.0"
    assert row["key"] == ""
    assert "Scalar input rate" in row["description"]


def test_synthetic_output_catalog_rows(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from scripts.i_o_tables import output_catalog_rows

    config = synthetic_configured_pipeline.config
    graph = synthetic_configured_pipeline.graph
    bindings = synthetic_configured_pipeline.series_bindings
    rows = output_catalog_rows(
        derive_output_series(graph, bindings, workbook=config.workbook_path),
        bindings,
    )
    assert [row["series_id"] for row in rows] == ["result_a", "result_b"]
    assert [row["address"] for row in rows] == ["Outputs!B1", "Outputs!C1"]
    assert [row["compute"] for row in rows] == [
        "compute_result_a",
        "compute_result_b",
    ]
    assert rows[0]["unit_measure"] == ""
    assert "First parallel output" in rows[0]["description"]
    assert rows[0]["key"] == ""


def test_output_catalog_includes_unit_measure() -> None:
    from scripts.i_o_tables import output_catalog_rows

    output_series: list[dict[str, Any]] = [
        {
            "id": "debt_path",
            "compute_name": "compute_debt_path",
            "key_fields": ["TIME_PERIOD"],
            "cells": [
                {
                    "address": "Outputs!B12",
                    "coordinates": {},
                    "key": {"TIME_PERIOD": 1},
                    "record": {"OBS_VALUE": None, "UNIT_MEASURE": "PC_GDP"},
                }
            ],
            "issues": [],
        }
    ]
    bindings = {
        "series": [
            {
                "id": "debt_path",
                "notes": "Debt-to-GDP path",
                "structure": {
                    "attributes": [{"concept": "UNIT_MEASURE", "value": "PC_GDP"}]
                },
            }
        ]
    }
    rows = output_catalog_rows(output_series, bindings)
    assert len(rows) == 1
    assert rows[0]["unit_measure"] == "PC_GDP"
    assert rows[0]["key"] == "TIME_PERIOD=1"
    assert rows[0]["description"] == "Debt-to-GDP path"


def test_emit_startup_site_writes_catalog_files(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
    tmp_path: Path,
) -> None:
    from scripts.i_o_tables import emit_startup_site

    output_dir = tmp_path / "startup-site"
    emit_startup_site(synthetic_configured_pipeline.config, output_dir=output_dir)

    expected = (
        output_dir / "startup-guide.csv",
        output_dir / "output-catalog.csv",
        output_dir / "index.html",
        output_dir / "inputs.html",
        output_dir / "outputs.html",
        output_dir / "statement-graph.html",
        output_dir
        / "download"
        / synthetic_configured_pipeline.config.workbook_path.name,
    )
    for path in expected:
        assert path.is_file(), path

    input_rows = list((output_dir / "startup-guide.csv").open(encoding="utf-8"))
    assert any("input_rate" in line and "Inputs!A1" in line for line in input_rows)

    with (output_dir / "output-catalog.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        output_rows = list(csv.DictReader(handle))
    assert {row["series_id"] for row in output_rows} == {"result_a", "result_b"}

    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert "inputs.html" in index_html
    assert "outputs.html" in index_html
    assert "statement-graph.html" in index_html
    assert "download/" in index_html
    assert "input_rate" in (output_dir / "inputs.html").read_text(encoding="utf-8")
    assert "compute_result_a" in (output_dir / "outputs.html").read_text(
        encoding="utf-8"
    )
    statement_html = (output_dir / "statement-graph.html").read_text(encoding="utf-8")
    assert "<html" in statement_html.lower()


def test_posix_serve_directory_is_git_bash_safe(
    tmp_path: Path,
) -> None:
    from scripts.i_o_tables import posix_serve_directory

    repo_root = tmp_path / "repo"
    output_dir = repo_root / "artifacts" / "startup-site"
    output_dir.mkdir(parents=True)
    served = posix_serve_directory(output_dir, repo_root=repo_root)
    assert served == "artifacts/startup-site"
    assert "\\" not in served


def test_cli_output_dir_writes_site_and_prints_posix_serve_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src import graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path, repo_root=tmp_path)
    cache_dir = tmp_path / "dependency-graph"
    monkeypatch.setattr(graph_cache, "DEFAULT_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr("scripts.i_o_tables.load_pipeline_config", lambda: config)
    monkeypatch.setattr(
        "scripts.i_o_tables.validate_pipeline_config", lambda _config: None
    )

    from scripts.i_o_tables import main

    output_dir = tmp_path / "site"
    assert main(["--output-dir", str(output_dir)]) == 0
    assert (output_dir / "index.html").is_file()
    assert (output_dir / "startup-guide.csv").is_file()
    printed = capsys.readouterr().out
    directory_flag = printed.split("--directory", 1)[1].split()[0]
    assert "\\" not in directory_flag
    assert "/" in directory_flag or directory_flag == "site"


def test_importing_module_does_not_run_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "scripts.i_o_tables.emit_startup_site",
        lambda *_args, **_kwargs: calls.append("emit"),
    )
    importlib.reload(importlib.import_module("scripts.i_o_tables"))
    assert calls == []
