"""Tests for the standalone input/output catalog script."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from excel_grapher.core.cell_types import Between, RealBetween

from scripts.i_o_tables import (
    describe_constraint,
    input_catalog_rows,
    main,
    output_catalog_rows,
    serve_directory_arg,
    write_startup_site,
)


def test_serve_directory_arg_is_posix_relative_inside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    output_dir = repo / "artifacts" / "startup-site"
    output_dir.mkdir(parents=True)
    assert serve_directory_arg(output_dir, repo) == "artifacts/startup-site"


def test_serve_directory_arg_is_posix_absolute_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output_dir = tmp_path / "elsewhere"
    output_dir.mkdir()
    hint = serve_directory_arg(output_dir, repo)
    assert "\\" not in hint
    assert Path(hint) == output_dir.resolve()


def test_describe_constraint_literal() -> None:
    dtype, acceptable = describe_constraint(Literal["Borvelia", "Litellia"])
    assert dtype == "str"
    assert acceptable == "Borvelia, Litellia"


def test_describe_constraint_between() -> None:
    dtype, acceptable = describe_constraint(Annotated[int, Between(1, 5)])
    assert dtype == "int"
    assert acceptable == "[1, 5]"


def test_describe_constraint_real_between() -> None:
    dtype, acceptable = describe_constraint(Annotated[float, RealBetween(0.0, 200.0)])
    assert dtype == "float"
    assert acceptable == "[0.0, 200.0]"


def test_describe_constraint_unknown_falls_back_to_repr() -> None:
    dtype, acceptable = describe_constraint(object())
    assert dtype.startswith("<")
    assert acceptable is None


def test_input_catalog_rows_use_domain_overlay(
    synthetic_configured_pipeline,
) -> None:
    pipeline = synthetic_configured_pipeline
    rows = input_catalog_rows(
        pipeline.graph,
        pipeline.series_bindings,
        config=pipeline.config,
    )
    assert [row["id"] for row in rows] == ["input_rate"]
    row = rows[0]
    assert row["range"] == "Inputs!A1"
    assert row["dtype"] == "float"
    assert row["acceptable"] == "[0.0, 100.0]"
    assert row["dimensions"] is None
    assert row["notes"] == "Scalar input rate for the synthetic smoke workbook."


def test_output_catalog_rows_include_notes(
    synthetic_configured_pipeline,
) -> None:
    pipeline = synthetic_configured_pipeline
    rows = output_catalog_rows(
        pipeline.graph,
        pipeline.series_bindings,
        config=pipeline.config,
    )
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {"result_a", "result_b"}
    assert by_id["result_a"]["range"] == "Outputs!B1"
    assert by_id["result_a"]["dtype"] == "float"
    assert by_id["result_a"]["unit"] is None
    assert by_id["result_a"]["notes"] == "First parallel output column."


def test_write_startup_site_emits_csv_html_and_workbook_copy(
    tmp_path,
    synthetic_configured_pipeline,
    monkeypatch,
) -> None:
    pipeline = synthetic_configured_pipeline
    monkeypatch.setattr(
        "scripts.i_o_tables.load_pipeline_dependency_graph",
        lambda _config: (pipeline.graph, "test-key"),
    )
    out = tmp_path / "startup-site"
    write_startup_site(pipeline.config, out)

    assert (out / "startup-guide.csv").is_file()
    assert (out / "output-catalog.csv").is_file()
    assert (out / "index.html").is_file()
    assert (out / "inputs.html").is_file()
    assert (out / "outputs.html").is_file()
    assert (out / "statement-graph.html").is_file()
    assert (out / "download" / pipeline.config.workbook_path.name).is_file()

    inputs_html = (out / "inputs.html").read_text(encoding="utf-8")
    outputs_html = (out / "outputs.html").read_text(encoding="utf-8")
    index_html = (out / "index.html").read_text(encoding="utf-8")
    guide_csv = (out / "startup-guide.csv").read_text(encoding="utf-8")
    assert "input_rate" in inputs_html
    assert "input_rate" in guide_csv
    assert "result_a" in outputs_html
    assert "Synthetic Model" in index_html
    assert 'src="statement-graph.html"' in index_html


def test_main_writes_to_output_dir(
    tmp_path,
    synthetic_configured_pipeline,
    monkeypatch,
) -> None:
    pipeline = synthetic_configured_pipeline
    monkeypatch.setattr(
        "scripts.i_o_tables.load_pipeline_config",
        lambda: pipeline.config,
    )
    monkeypatch.setattr(
        "scripts.i_o_tables.validate_pipeline_config",
        lambda _config: None,
    )
    monkeypatch.setattr(
        "scripts.i_o_tables.load_pipeline_dependency_graph",
        lambda _config: (pipeline.graph, "test-key"),
    )
    out = tmp_path / "site"
    assert main(["--output-dir", str(out)]) == 0
    assert (out / "index.html").is_file()
    assert (out / "startup-guide.csv").is_file()
    assert (out / "output-catalog.csv").is_file()
