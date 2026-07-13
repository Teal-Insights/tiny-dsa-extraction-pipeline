"""Tests for binding/cache utility scripts and helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from excel_grapher.series_bindings import load_series_bindings

from scripts.internal_binding_burndown import load_graph
from scripts.regenerate_graph_cache import (
    graph_cache_target_bundles,
    regenerate_graph_cache,
)
from src.binding_authoring import (
    build_binding_documents,
    emit_bindings_from_catalog,
    load_binding_catalog,
)
from src.graph_cache import (
    dependency_graph_cache_key,
    load_dependency_graph,
    prune_stale_graph_cache_entries,
)
from src.internal_binding_coverage import (
    contiguous_column_ranges,
    find_unbound_internal_formula_cells_from_manifest,
    format_row_column_spans,
    group_unbound_cells_by_sheet_row,
    suggested_layout_for_row,
)
from tests.fixtures.synthetic_pipeline import (
    synthetic_pipeline_config,
    write_synthetic_workbook,
)


def test_contiguous_column_ranges_groups_gaps() -> None:
    assert contiguous_column_ranges([2, 3, 4, 7, 8]) == [(2, 4), (7, 8)]


def test_group_unbound_cells_by_sheet_row() -> None:
    grouped = group_unbound_cells_by_sheet_row(("Engine!B2", "Engine!C2", "Outputs!B1"))
    assert grouped == {"Engine": {2: [2, 3]}, "Outputs": {1: [2]}}


def test_format_row_column_spans() -> None:
    assert (
        format_row_column_spans(sheet="Engine", row=2, columns=[2, 3, 4])
        == "Engine!B2:D2"
    )
    assert (
        format_row_column_spans(sheet="Engine", row=2, columns=[2, 4])
        == "Engine!B2, Engine!D2"
    )


def test_suggested_layout_for_row() -> None:
    assert suggested_layout_for_row([2]) == "scalar"
    assert suggested_layout_for_row([2, 3]) == "row_series"


def test_graph_cache_target_bundles_includes_default_and_extras(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = replace(
        synthetic_pipeline_config(workbook_path=workbook_path),
        graph_cache_target_bundles=(("subset graph", ("Outputs!B1",)),),
    )
    bundles = graph_cache_target_bundles(config)
    assert bundles[0] == ("default graph", config.targets)
    assert bundles[1] == ("subset graph", ("Outputs!B1",))


def test_regenerate_graph_cache_builds_and_prunes_stale_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.graph_cache as graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"
    cache_dir.mkdir(parents=True)
    (cache_dir / "stale-key.pkl.gz").write_bytes(b"stale")
    (cache_dir / "stale-key.meta.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(graph_cache, "COMMITTED_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.load_pipeline_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.validate_pipeline_config",
        lambda _config: None,
    )

    current_keys = regenerate_graph_cache(force=True)
    assert current_keys
    assert not (cache_dir / "stale-key.pkl.gz").is_file()
    for cache_key in current_keys:
        assert load_dependency_graph(cache_key, cache_dir=cache_dir) is not None


def test_committed_graph_cache_is_fresh_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.graph_cache as graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"

    monkeypatch.setattr(graph_cache, "COMMITTED_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.load_pipeline_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.validate_pipeline_config",
        lambda _config: None,
    )

    current_keys = regenerate_graph_cache(force=True)
    expected_key = dependency_graph_cache_key(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        bindings_path=config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert expected_key in current_keys
    assert load_dependency_graph(expected_key, cache_dir=cache_dir) is not None


def test_prune_stale_graph_cache_entries_removes_only_stale_keys(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "dependency-graph"
    cache_dir.mkdir()
    keep = cache_dir / "keep.pkl.gz"
    drop = cache_dir / "drop.pkl.gz"
    keep.write_bytes(b"keep")
    drop.write_bytes(b"drop")

    pruned = prune_stale_graph_cache_entries({"keep"}, cache_dir=cache_dir)

    assert pruned == ["drop.pkl.gz"]
    assert keep.is_file()
    assert not drop.is_file()


def test_internal_binding_burndown_groups_unbound_formula_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.graph_cache as graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    fixture_bindings = Path(__file__).resolve().parent / "fixtures" / "synthetic"
    for name in ("inputs.bindings.yaml", "outputs.bindings.yaml"):
        source = fixture_bindings / name
        (bindings_path / name).write_text(source.read_text(encoding="utf-8"))

    config = replace(
        synthetic_pipeline_config(workbook_path=workbook_path),
        bindings_path=bindings_path,
    )
    cache_dir = tmp_path / "dependency-graph"

    monkeypatch.setattr(graph_cache, "DEFAULT_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "scripts.internal_binding_burndown.DEFAULT_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.load_pipeline_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.validate_pipeline_config",
        lambda _config: None,
    )
    regenerate_graph_cache(force=True)

    graph, _cache_key = load_graph(config)
    bindings = load_series_bindings(config.bindings_path)
    unbound = find_unbound_internal_formula_cells_from_manifest(
        graph=graph,
        bindings=bindings,
        exempt_cells=config.internal_binding_exempt_cells,
        workbook=config.workbook_path,
    )

    assert unbound == ("Engine!B2", "Engine!C2")
    grouped = group_unbound_cells_by_sheet_row(unbound)
    assert grouped == {"Engine": {2: [2, 3]}}
    assert (
        format_row_column_spans(sheet="Engine", row=2, columns=grouped["Engine"][2])
        == "Engine!B2:C2"
    )
    assert suggested_layout_for_row(grouped["Engine"][2]) == "row_series"


def test_internal_binding_burndown_warns_when_cached_graph_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.graph_cache as graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    fixture_bindings = Path(__file__).resolve().parent / "fixtures" / "synthetic"
    for name in (
        "inputs.bindings.yaml",
        "outputs.bindings.yaml",
        "internals.bindings.yaml",
    ):
        source = fixture_bindings / name
        (bindings_path / name).write_text(source.read_text(encoding="utf-8"))

    config = replace(
        synthetic_pipeline_config(workbook_path=workbook_path),
        bindings_path=bindings_path,
    )
    cache_dir = tmp_path / "dependency-graph"

    monkeypatch.setattr(graph_cache, "DEFAULT_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "scripts.internal_binding_burndown.DEFAULT_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.load_pipeline_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.validate_pipeline_config",
        lambda _config: None,
    )
    regenerate_graph_cache(force=True)

    internals_path = bindings_path / "internals.bindings.yaml"
    internals_path.write_text(
        internals_path.read_text(encoding="utf-8") + "\n# cache-bust\n",
        encoding="utf-8",
    )

    load_graph(config)
    captured = capsys.readouterr()

    assert "Warning: newest cached graph key does not match" in captured.out
    assert "regenerate_graph_cache" in captured.out


def test_internal_binding_burndown_reports_no_unbound_cells_for_synthetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.graph_cache as graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"

    monkeypatch.setattr(graph_cache, "DEFAULT_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "scripts.internal_binding_burndown.DEFAULT_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.internal_binding_burndown.load_pipeline_config",
        lambda: config,
    )

    graph, _cache_key = load_graph(config)
    bindings = load_series_bindings(config.bindings_path)
    unbound = find_unbound_internal_formula_cells_from_manifest(
        graph=graph,
        bindings=bindings,
        exempt_cells=config.internal_binding_exempt_cells,
        workbook=config.workbook_path,
    )
    assert unbound == ()


def test_binding_catalog_roundtrip_matches_synthetic_fixtures(
    tmp_path: Path,
) -> None:
    catalog_path = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "binding-catalog.example.yaml"
    )
    catalog = load_binding_catalog(catalog_path)
    documents = build_binding_documents(catalog)

    for direction in ("inputs", "outputs", "internals"):
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "synthetic"
            / f"{direction}.bindings.yaml"
        )
        emitted = documents[f"{direction}.bindings.yaml"]
        expected = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        assert emitted["schema_version"] == expected["schema_version"]
        assert emitted["series"] == expected["series"]


def test_emit_bindings_from_catalog_validates_against_synthetic_workbook(
    tmp_path: Path,
) -> None:
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    bindings_dir = tmp_path / "bindings"
    catalog_path = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "binding-catalog.example.yaml"
    )

    written, validation = emit_bindings_from_catalog(
        catalog_path=catalog_path,
        bindings_dir=bindings_dir,
        workbook_path=config.workbook_path,
        validate=True,
    )

    assert len(written) == 3
    assert validation is not None
    assert validation["report"]["ok"] is True
