"""Tests for binding/cache utility scripts and helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import fastpyxl
import pytest
import yaml
from excel_grapher.grapher.graph import DependencyGraph
from excel_grapher.series_bindings import load_series_bindings
from excel_grapher.series_bindings.resolve import BindingDirection

from scripts.regenerate_graph_cache import (
    graph_cache_target_bundles,
    regenerate_graph_cache,
)
from src.binding_authoring import (
    build_binding_documents,
    emit_bindings_from_catalog,
    load_binding_catalog,
)
from src.binding_resolution_audit import (
    AuditFinding,
    _unfilled_label_binds,
    audit_binding_resolutions,
    find_duplicate_internal_formula_cell_bindings,
    find_sparse_label_bind_issues,
    findings_from_resolution,
    format_audit_findings,
)
from src.bindings_validation_cache import COMMITTED_BINDINGS_VALIDATION_CACHE_DIR
from src.graph_cache import (
    dependency_graph_cache_key,
    load_dependency_graph,
    load_pipeline_dependency_graph,
    prune_stale_graph_cache_entries,
    save_dependency_graph,
)
from src.internal_binding_coverage import (
    collapse_unbound_cells_to_ranges,
    contiguous_column_ranges,
    find_unbound_internal_formula_cells_from_manifest,
    format_row_column_spans,
    group_unbound_cells_by_sheet_row,
    suggested_layout_for_row,
)
from src.pipeline_config import PipelineConfig
from src.series_derived_cache import COMMITTED_SERIES_DERIVED_CACHE_DIR
from src.series_resolution_cache import COMMITTED_SERIES_RESOLUTION_CACHE_DIR
from tests.fixtures.synthetic_pipeline import (
    synthetic_pipeline_config,
    write_synthetic_workbook,
)
from tests.fixtures.test_state import (
    REPO_BINDINGS_VALIDATION_CACHE_DIR,
    REPO_SERIES_DERIVED_CACHE_DIR,
    REPO_SERIES_RESOLUTION_CACHE_DIR,
)

_SPARSE_YEARS_SERIES: dict[str, Any] = {
    "id": "engine_sparse_years",
    "sheet": "Engine",
    "data_range": "Engine!B2:C2",
    "layout": "series",
    "internal": {},
    "structure": {
        "measure": {
            "concept": "OBS_VALUE",
            "dtype": "float",
            "bind": {"kind": "data_cell", "read": "float"},
        },
        "dimensions": [
            {
                "id": "TIME_PERIOD",
                "concept": "TIME_PERIOD",
                "role": "key",
                "scope": "cell",
                "bind": {
                    "kind": "column_header",
                    "header_row": 1,
                    "read": "int",
                },
            }
        ],
    },
    "key": ["TIME_PERIOD"],
    "validation": {"warn_on_partial_overlap": False},
}


@dataclass(frozen=True)
class SparseYearsAuditFixture:
    workbook_path: Path
    bindings_path: Path
    config: PipelineConfig
    cache_dir: Path
    series: dict[str, Any]


def _monkeypatch_temp_graph_cache(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cache_dir: Path,
    config: PipelineConfig,
    patch_audit_cli: bool = False,
    series_cache_dir: Path | None = None,
    derived_cache_dir: Path | None = None,
    validation_cache_dir: Path | None = None,
) -> Path:
    """Redirect graph + series/validation committed caches away from the repo.

    ``regenerate_graph_cache(force=True)`` clears and rewrites
    ``COMMITTED_SERIES_RESOLUTION_CACHE_DIR``,
    ``COMMITTED_SERIES_DERIVED_CACHE_DIR``, and
    ``COMMITTED_BINDINGS_VALIDATION_CACHE_DIR``; tests that only redirect the
    graph cache would wipe the committed artifacts used by session fixtures.
    """
    from src import (
        bindings_validation_cache,
        graph_cache,
        series_derived_cache,
        series_resolution_cache,
    )

    resolved_series_cache_dir = (
        series_cache_dir
        if series_cache_dir is not None
        else cache_dir.parent / "series-resolution"
    )
    resolved_derived_cache_dir = (
        derived_cache_dir
        if derived_cache_dir is not None
        else cache_dir.parent / "series-derived"
    )
    resolved_validation_cache_dir = (
        validation_cache_dir
        if validation_cache_dir is not None
        else cache_dir.parent / "bindings-validation"
    )
    monkeypatch.setattr(graph_cache, "DEFAULT_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(graph_cache, "COMMITTED_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        series_resolution_cache,
        "DEFAULT_SERIES_RESOLUTION_CACHE_DIR",
        resolved_series_cache_dir,
    )
    monkeypatch.setattr(
        series_resolution_cache,
        "COMMITTED_SERIES_RESOLUTION_CACHE_DIR",
        resolved_series_cache_dir,
    )
    monkeypatch.setattr(
        series_derived_cache,
        "DEFAULT_SERIES_DERIVED_CACHE_DIR",
        resolved_derived_cache_dir,
    )
    monkeypatch.setattr(
        series_derived_cache,
        "COMMITTED_SERIES_DERIVED_CACHE_DIR",
        resolved_derived_cache_dir,
    )
    monkeypatch.setattr(
        bindings_validation_cache,
        "DEFAULT_BINDINGS_VALIDATION_CACHE_DIR",
        resolved_validation_cache_dir,
    )
    monkeypatch.setattr(
        bindings_validation_cache,
        "COMMITTED_BINDINGS_VALIDATION_CACHE_DIR",
        resolved_validation_cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_GRAPH_CACHE_DIR",
        cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_SERIES_RESOLUTION_CACHE_DIR",
        resolved_series_cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_SERIES_DERIVED_CACHE_DIR",
        resolved_derived_cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.COMMITTED_BINDINGS_VALIDATION_CACHE_DIR",
        resolved_validation_cache_dir,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.load_pipeline_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "scripts.regenerate_graph_cache.validate_pipeline_config",
        lambda _config: None,
    )
    if patch_audit_cli:
        monkeypatch.setattr(
            "scripts.binding_resolution_audit.load_pipeline_config",
            lambda: config,
        )
        monkeypatch.setattr(
            "scripts.binding_resolution_audit.validate_pipeline_config",
            lambda _config: None,
        )
    return resolved_series_cache_dir


def _prepare_sparse_years_audit_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_audit_cli: bool = False,
) -> SparseYearsAuditFixture:
    """Synthetic workbook with sparse year headers and an unfilled column_header bind."""
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    wb = fastpyxl.load_workbook(workbook_path)
    wb["Engine"]["B1"] = 2020
    wb["Engine"]["C1"] = None
    wb.save(workbook_path)
    wb.close()

    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    fixture_bindings = Path(__file__).resolve().parent / "fixtures" / "synthetic"
    for name in (
        "inputs.bindings.yaml",
        "outputs.bindings.yaml",
        "internals.bindings.yaml",
    ):
        (bindings_path / name).write_text(
            (fixture_bindings / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    internals = yaml.safe_load(
        (bindings_path / "internals.bindings.yaml").read_text(encoding="utf-8")
    )
    series = dict(_SPARSE_YEARS_SERIES)
    internals["series"].append(series)
    (bindings_path / "internals.bindings.yaml").write_text(
        yaml.safe_dump(internals, sort_keys=False),
        encoding="utf-8",
    )

    config = replace(
        synthetic_pipeline_config(workbook_path=workbook_path),
        bindings_path=bindings_path,
    )
    cache_dir = tmp_path / "dependency-graph"
    _monkeypatch_temp_graph_cache(
        monkeypatch,
        cache_dir=cache_dir,
        config=config,
        patch_audit_cli=patch_audit_cli,
    )
    regenerate_graph_cache(force=True)
    return SparseYearsAuditFixture(
        workbook_path=workbook_path,
        bindings_path=bindings_path,
        config=config,
        cache_dir=cache_dir,
        series=series,
    )


def test_contiguous_column_ranges_groups_gaps() -> None:
    assert contiguous_column_ranges([2, 3, 4, 7, 8]) == [(2, 4), (7, 8)]


def test_group_unbound_cells_by_sheet_row() -> None:
    grouped = group_unbound_cells_by_sheet_row(("Engine!B2", "Engine!C2", "Outputs!B1"))
    assert grouped == {"Engine": {2: [2, 3]}, "Outputs": {1: [2]}}


def test_group_unbound_cells_by_sheet_row_strips_quoted_sheet_names() -> None:
    grouped = group_unbound_cells_by_sheet_row(("'Climate Database'!B26",))
    assert grouped == {"Climate Database": {26: [2]}}


def test_collapse_unbound_cells_to_ranges_joins_consecutive_rows() -> None:
    cells = ("Engine!B2", "Engine!C2", "Engine!B3", "Engine!C3", "Outputs!B1")
    assert collapse_unbound_cells_to_ranges(cells) == (
        "Engine!B2:C3",
        "Outputs!B1",
    )


def test_collapse_unbound_cells_to_ranges_splits_column_gaps() -> None:
    cells = ("Store!B2", "Store!D2", "Store!B3", "Store!D3")
    assert collapse_unbound_cells_to_ranges(cells) == (
        "Store!B2:B3",
        "Store!D2:D3",
    )


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
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"
    cache_dir.mkdir(parents=True)
    (cache_dir / "stale-key.pkl.gz").write_bytes(b"stale")
    (cache_dir / "stale-key.meta.json").write_text("{}", encoding="utf-8")

    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)

    current_keys = regenerate_graph_cache(force=True)
    assert current_keys
    assert not (cache_dir / "stale-key.pkl.gz").is_file()
    for cache_key in current_keys:
        assert load_dependency_graph(cache_key, cache_dir=cache_dir) is not None


def test_regenerate_graph_cache_does_not_touch_committed_validation_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic regenerate must not clear or prune the repo validation cache."""
    assert COMMITTED_BINDINGS_VALIDATION_CACHE_DIR == REPO_BINDINGS_VALIDATION_CACHE_DIR
    REPO_BINDINGS_VALIDATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    before = sorted(p.name for p in COMMITTED_BINDINGS_VALIDATION_CACHE_DIR.iterdir())

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"
    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)

    regenerate_graph_cache(force=True)

    after = sorted(p.name for p in COMMITTED_BINDINGS_VALIDATION_CACHE_DIR.iterdir())
    assert after == before


def test_synthetic_regenerate_leaves_committed_series_resolution_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic force-regenerate must not wipe the repo series-resolution cache."""
    assert COMMITTED_SERIES_RESOLUTION_CACHE_DIR == REPO_SERIES_RESOLUTION_CACHE_DIR

    committed_series_dir = COMMITTED_SERIES_RESOLUTION_CACHE_DIR
    committed_series_dir.mkdir(parents=True, exist_ok=True)
    sentinel = committed_series_dir / "committed-sentinel.meta.json"
    sentinel_created = False
    if not sentinel.is_file():
        sentinel.write_text("{}\n", encoding="utf-8")
        sentinel_created = True
    before = sorted(path.name for path in committed_series_dir.iterdir())

    try:
        workbook_path = tmp_path / "workbook.xlsx"
        write_synthetic_workbook(workbook_path)
        config = synthetic_pipeline_config(workbook_path=workbook_path)
        cache_dir = tmp_path / "dependency-graph"
        _monkeypatch_temp_graph_cache(
            monkeypatch,
            cache_dir=cache_dir,
            config=config,
        )
        regenerate_graph_cache(force=True)

        after = sorted(path.name for path in committed_series_dir.iterdir())
        assert after == before
    finally:
        if sentinel_created:
            sentinel.unlink(missing_ok=True)


def test_synthetic_regenerate_leaves_committed_series_derived_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic force-regenerate must not wipe the repo series-derived cache."""
    assert COMMITTED_SERIES_DERIVED_CACHE_DIR == REPO_SERIES_DERIVED_CACHE_DIR

    committed_derived_dir = COMMITTED_SERIES_DERIVED_CACHE_DIR
    committed_derived_dir.mkdir(parents=True, exist_ok=True)
    sentinel = committed_derived_dir / "committed-sentinel.meta.json"
    sentinel_created = False
    if not sentinel.is_file():
        sentinel.write_text("{}\n", encoding="utf-8")
        sentinel_created = True
    before = sorted(path.name for path in committed_derived_dir.iterdir())

    try:
        workbook_path = tmp_path / "workbook.xlsx"
        write_synthetic_workbook(workbook_path)
        config = synthetic_pipeline_config(workbook_path=workbook_path)
        cache_dir = tmp_path / "dependency-graph"
        _monkeypatch_temp_graph_cache(
            monkeypatch,
            cache_dir=cache_dir,
            config=config,
        )
        regenerate_graph_cache(force=True)

        after = sorted(path.name for path in committed_derived_dir.iterdir())
        assert after == before
    finally:
        if sentinel_created:
            sentinel.unlink(missing_ok=True)


def test_committed_graph_cache_is_fresh_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"

    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)

    current_keys = regenerate_graph_cache(force=True)
    expected_key = dependency_graph_cache_key(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
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

    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)
    regenerate_graph_cache(force=True)

    graph, _cache_key = load_pipeline_dependency_graph(config)
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
    assert collapse_unbound_cells_to_ranges(unbound) == ("Engine!B2:C2",)


def test_internal_binding_burndown_does_not_warn_when_only_bindings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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

    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)
    regenerate_graph_cache(force=True)

    internals_path = bindings_path / "internals.bindings.yaml"
    internals_path.write_text(
        internals_path.read_text(encoding="utf-8") + "\n# cache-bust\n",
        encoding="utf-8",
    )

    load_pipeline_dependency_graph(config)
    captured = capsys.readouterr()

    assert "Warning: newest cached graph key does not match" not in captured.out


def test_internal_binding_burndown_warns_when_cached_graph_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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

    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)
    regenerate_graph_cache(force=True)

    workbook_path.write_bytes(workbook_path.read_bytes() + b"changed")

    load_pipeline_dependency_graph(config)
    captured = capsys.readouterr()

    assert "Warning: newest cached graph key does not match" in captured.out
    assert "regenerate_graph_cache" in captured.out
    warning_line = captured.out.split("Warning:", 1)[1].split("\n", 1)[0]
    assert "bindings" not in warning_line


def test_load_pipeline_dependency_graph_prefers_fingerprint_match_over_newer_stale_pickle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"
    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)
    regenerate_graph_cache(force=True)

    expected_key = dependency_graph_cache_key(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
    )
    matched_graph = load_dependency_graph(expected_key, cache_dir=cache_dir)
    assert matched_graph is not None

    stale_key = "zzzz-newer-stale-key"
    save_dependency_graph(
        matched_graph,
        cache_key=stale_key,
        workbook_path=config.workbook_path,
        targets=config.targets,
        cache_dir=cache_dir,
    )
    stale_payload = cache_dir / f"{stale_key}.pkl.gz"
    matched_payload = cache_dir / f"{expected_key}.pkl.gz"
    newer_mtime = matched_payload.stat().st_mtime + 10
    os.utime(stale_payload, (newer_mtime, newer_mtime))

    graph, cache_key = load_pipeline_dependency_graph(config)
    captured = capsys.readouterr()

    assert cache_key == expected_key
    assert len(graph) == len(matched_graph)
    assert "Warning: newest cached graph key does not match" not in captured.out


def test_internal_binding_burndown_reports_no_unbound_cells_for_synthetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import graph_cache

    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"

    monkeypatch.setattr(graph_cache, "DEFAULT_GRAPH_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "scripts.internal_binding_burndown.load_pipeline_config",
        lambda: config,
    )

    graph, _cache_key = load_pipeline_dependency_graph(config)
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

    for direction in ("inputs", "outputs", "internals", "constants"):
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


def test_measure_shard_pattern_catalog_emits_filled_gap_column_shards() -> None:
    """Pedagogical filled-header + measure-shard catalog stays structurally valid."""
    catalog_path = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "binding-pattern-measure-shards.example.yaml"
    )
    catalog = load_binding_catalog(catalog_path)
    documents = build_binding_documents(catalog)
    outputs = documents["outputs.bindings.yaml"]["series"]
    internals = documents["internals.bindings.yaml"]["series"]

    concept_ids = {concept["id"] for concept in catalog["concept_scheme"]["concepts"]}
    assert {"OBS_VALUE", "TIME_PERIOD", "SCENARIO", "MEASURE"} <= concept_ids

    assert {series["id"] for series in outputs} == {
        "gap_milestones_2050",
        "gap_milestones_2075",
    }
    compute_names = {series["output"]["compute"]["name"] for series in outputs}
    assert compute_names == {"compute_gap_milestones"}
    for series in outputs:
        time_binds = [
            dim["bind"]
            for dim in series["structure"]["dimensions"]
            if dim["id"] == "TIME_PERIOD"
        ]
        assert len(time_binds) == 1
        assert time_binds[0]["fill"] is True
        assert time_binds[0]["kind"] == "column_header"

    assert len(internals) == 1
    assert set(internals[0]["key"]) == {"SCENARIO", "TIME_PERIOD", "MEASURE"}


def test_binding_guidance_documents_unique_vs_shared_compute_names() -> None:
    """Authoring materials teach when shared names merge vs when they mask paths."""
    root = Path(__file__).resolve().parents[1]
    readme = (root / "bindings" / "README.md").read_text(encoding="utf-8")
    prompt = (root / "templates" / "binding-authoring-prompt.txt").read_text(
        encoding="utf-8"
    )
    example = (
        root / "templates" / "binding-pattern-measure-shards.example.yaml"
    ).read_text(encoding="utf-8")

    for text in (readme, prompt, example):
        lowered = text.lower()
        assert "unreachable" in lowered
        assert "unique" in lowered or "uniquify" in lowered
        assert "share" in lowered or "shared" in lowered

    assert "output.compute.name" in readme
    assert "compute_/set_" in prompt or "compute_" in prompt


def test_binding_guidance_documents_constant_direction() -> None:
    """Authoring materials teach constant: {} for reader-only graph leaves."""
    root = Path(__file__).resolve().parents[1]
    bindings_readme = (root / "bindings" / "README.md").read_text(encoding="utf-8")
    pipeline_readme = (root / "README.md").read_text(encoding="utf-8")
    prompt = (root / "templates" / "binding-authoring-prompt.txt").read_text(
        encoding="utf-8"
    )

    for text in (bindings_readme, pipeline_readme, prompt):
        lowered = text.lower()
        assert "constant: {}" in text
        assert "constants.bindings.yaml" in lowered

    assert "keyword-only" in bindings_readme
    assert "data.py" in bindings_readme.lower()
    assert "compute_*" in bindings_readme
    assert "non_leaf_constant_overlap" in bindings_readme
    assert "bind.kind: constant" in bindings_readme
    assert (
        "derive_constant_series" in pipeline_readme
        or "derive_constant_series" in prompt
    )

    for text in (pipeline_readme, prompt):
        lowered = text.lower()
        assert "input: {}" in text
        assert "xl_cell" in lowered
        assert "constant.reader" not in lowered


def test_excel_grapher_floor_is_21_5_0() -> None:
    """Lockfile and pyproject must agree on excel-grapher>=21.5.0."""
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (root / "uv.lock").read_text(encoding="utf-8")
    installed = tuple(int(part) for part in version("excel-grapher").split(".")[:3])

    assert "excel-grapher>=21.5.0" in pyproject
    assert '{ name = "excel-grapher", specifier = ">=21.5.0" }' in lockfile
    assert installed >= (21, 5, 0)


def test_binding_resolution_audit_uses_public_apply_series_excludes() -> None:
    """Audit must call the public exclude API, not the private helper."""
    from excel_grapher.series_bindings.ranges import apply_series_excludes

    import src.binding_resolution_audit as audit_module

    source = Path(audit_module.__file__).read_text(encoding="utf-8")
    assert apply_series_excludes is audit_module.apply_series_excludes
    assert "_apply_exclude_rows" not in source


def test_binding_resolution_audit_directions_include_constant() -> None:
    from src.binding_resolution_audit import DIRECTIONS

    assert DIRECTIONS == ("input", "output", "internal", "constant")


def test_binding_catalog_emits_constants_sidecar() -> None:
    catalog_path = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "binding-catalog.example.yaml"
    )
    documents = build_binding_documents(load_binding_catalog(catalog_path))
    assert "constants.bindings.yaml" in documents
    constants = documents["constants.bindings.yaml"]["series"]
    assert len(constants) == 1
    assert constants[0]["id"] == "input_bias"
    assert constants[0]["constant"] == {}
    assert "input" not in constants[0]
    assert "output" not in constants[0]
    assert "internal" not in constants[0]


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

    assert len(written) == 4
    assert validation is not None
    assert validation["report"]["ok"] is True


def test_findings_from_resolution_flags_partial_bind_and_empty_public() -> None:
    partial = findings_from_resolution(
        {
            "series_id": "gap_milestones",
            "ok": False,
            "requires_address": False,
            "leaves": [
                {
                    "address": "Sheet!I26",
                    "coordinates": {},
                    "key": {},
                    "record": {},
                }
            ],
            "issues": [
                {
                    "level": "error",
                    "code": "bind_resolution_failed",
                    "message": "column_header row 23: no source label",
                    "series_id": "gap_milestones",
                    "address": "Sheet!H26",
                }
            ],
        },
        direction="output",
        series={
            "id": "gap_milestones",
            "output": {"compute": {"name": "compute_gap"}},
        },
    )
    assert any(finding.code == "bind_resolution_failed" for finding in partial)
    assert any(finding.code == "partial_bind_failure" for finding in partial)

    empty = findings_from_resolution(
        {
            "series_id": "missing_output",
            "ok": True,
            "requires_address": False,
            "leaves": [],
            "issues": [
                {
                    "level": "warning",
                    "code": "no_resolved_cells",
                    "message": "No resolved output cells",
                    "series_id": "missing_output",
                    "address": None,
                }
            ],
        },
        direction="output",
        series={
            "id": "missing_output",
            "output": {"compute": {"name": "compute_missing"}},
        },
    )
    assert any(
        finding.code == "empty_public_series" and finding.severity == "warning"
        for finding in empty
    )

    empty_input = findings_from_resolution(
        {
            "series_id": "missing_input",
            "ok": True,
            "requires_address": False,
            "leaves": [],
            "issues": [
                {
                    "level": "warning",
                    "code": "no_resolved_cells",
                    "message": "No resolved input cells",
                    "series_id": "missing_input",
                    "address": None,
                }
            ],
        },
        direction="input",
        series={
            "id": "missing_input",
            "input": {},
        },
    )
    assert any(
        finding.code == "empty_public_series" and finding.severity == "warning"
        for finding in empty_input
    )


def test_unfilled_label_binds_skips_filled_and_non_label_binds() -> None:
    assert (
        _unfilled_label_binds(
            {
                "structure": {
                    "dimensions": [
                        {
                            "bind": {
                                "kind": "column_header",
                                "header_row": 1,
                                "fill": True,
                            }
                        },
                        {"bind": {"kind": "data_cell", "read": "float"}},
                    ]
                }
            }
        )
        == []
    )
    binds = _unfilled_label_binds(
        {
            "structure": {
                "dimensions": [
                    {"bind": {"kind": "column_header", "header_row": 1}},
                    {"bind": {"kind": "row_label", "label_column": "B"}},
                ]
            }
        }
    )
    assert len(binds) == 2


def test_find_sparse_label_bind_issues_short_circuits_without_workbook_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_expand(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("expand should not run when no unfilled label binds")

    def _fail_load(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("workbook should not load when no unfilled label binds")

    monkeypatch.setattr(
        "src.binding_resolution_audit.expand_data_range_for_graph",
        _fail_expand,
    )
    monkeypatch.setattr(
        "src.binding_resolution_audit.fastpyxl.load_workbook",
        _fail_load,
    )
    findings = find_sparse_label_bind_issues(
        graph=cast(DependencyGraph, None),  # unused on short-circuit
        series={
            "id": "scalar_only",
            "data_range": "Engine!B2",
            "structure": {
                "dimensions": [{"bind": {"kind": "data_cell", "read": "float"}}]
            },
        },
        workbook_path="unused.xlsx",
        direction="internal",
    )
    assert findings == []


def test_audit_binding_resolutions_reuses_one_workbook_for_sparse_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.binding_resolution_audit as audit_mod

    fixture = _prepare_sparse_years_audit_fixture(tmp_path, monkeypatch)
    graph, _ = load_pipeline_dependency_graph(fixture.config)

    sparse_workbook_handles: list[fastpyxl.Workbook | None] = []
    real_sparse = audit_mod.find_sparse_label_bind_issues

    def tracking_sparse(
        graph: DependencyGraph,
        series: dict[str, Any],
        *,
        workbook_path: Path | str,
        direction: BindingDirection,
        workbook: fastpyxl.Workbook | None = None,
    ) -> list[AuditFinding]:
        sparse_workbook_handles.append(workbook)
        return real_sparse(
            graph,
            series,
            workbook_path=workbook_path,
            direction=direction,
            workbook=workbook,
        )

    monkeypatch.setattr(audit_mod, "find_sparse_label_bind_issues", tracking_sparse)
    report = audit_binding_resolutions(
        graph,
        load_series_bindings(fixture.bindings_path),
        workbook=fixture.workbook_path,
        directions=("internal",),
    )
    assert not report.ok
    assert sparse_workbook_handles
    assert all(handle is not None for handle in sparse_workbook_handles)
    assert len({id(handle) for handle in sparse_workbook_handles}) == 1


def test_find_sparse_label_bind_issues_without_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepare_sparse_years_audit_fixture(tmp_path, monkeypatch)
    graph, _ = load_pipeline_dependency_graph(fixture.config)

    sparse = find_sparse_label_bind_issues(
        graph,
        fixture.series,
        workbook_path=fixture.workbook_path,
        direction="internal",
    )
    assert sparse
    assert sparse[0].code == "sparse_label_without_fill"
    assert "fill: true" in sparse[0].message

    report = audit_binding_resolutions(
        graph,
        load_series_bindings(fixture.bindings_path),
        workbook=fixture.workbook_path,
        directions=("internal",),
    )
    assert not report.ok
    sparse_findings = [
        finding
        for finding in report.findings
        if finding.series_id == "engine_sparse_years"
        and finding.code == "sparse_label_without_fill"
    ]
    assert sparse_findings
    assert any(
        finding.series_id == "engine_sparse_years"
        and finding.code in {"bind_resolution_failed", "partial_bind_failure"}
        for finding in report.findings
    )
    rendered = "\n".join(format_audit_findings(report.findings))
    assert "engine_sparse_years" in rendered
    assert "sparse_label_without_fill" in rendered


def test_duplicate_internal_audit_expands_list_data_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hole-split ``data_range`` lists must still participate in ownership."""
    import src.binding_resolution_audit as audit_module

    class _FakeNode:
        is_leaf = False
        normalized_formula = "=1"

    class _FakeGraph:
        def get_node(self, _address: str) -> _FakeNode:
            return _FakeNode()

    monkeypatch.setattr(
        audit_module,
        "expand_data_range_for_graph",
        lambda _graph, data_range, workbook=None: (
            ("Engine!B2",)
            if data_range == "Engine!B2"
            else (("Engine!C2",) if data_range == "Engine!C2" else ())
        ),
    )
    bindings = cast(
        Any,
        {
            "series": [
                {
                    "id": "left_span",
                    "data_range": ["Engine!B2", "Engine!C2"],
                    "internal": {},
                },
                {
                    "id": "right_cell",
                    "data_range": "Engine!B2",
                    "internal": {},
                },
            ]
        },
    )
    findings = find_duplicate_internal_formula_cell_bindings(
        cast(DependencyGraph, _FakeGraph()),
        bindings,
        workbook_path=Path("unused.xlsx"),
    )
    assert findings
    assert findings[0].code == "duplicate_internal_cell_binding"
    assert findings[0].address == "Engine!B2"
    assert "left_span" in findings[0].message
    assert "right_cell" in findings[0].message


def test_duplicate_internal_audit_respects_exclude_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping data_ranges are not duplicates when exclude_rows carve out cells."""
    import src.binding_resolution_audit as audit_module

    class _FakeNode:
        is_leaf = False
        normalized_formula = "=1"

    class _FakeGraph:
        def get_node(self, _address: str) -> _FakeNode:
            return _FakeNode()

    monkeypatch.setattr(
        audit_module,
        "expand_data_range_for_graph",
        lambda _graph, data_range, workbook=None: (
            ("Engine!B2", "Engine!B3", "Engine!B4", "Engine!B5")
            if data_range == "Engine!B2:B5"
            else (("Engine!B4",) if data_range == "Engine!B4" else ())
        ),
    )
    bindings = cast(
        Any,
        {
            "series": [
                {
                    "id": "row_series",
                    "data_range": "Engine!B4",
                    "internal": {},
                },
                {
                    "id": "column_series",
                    "data_range": "Engine!B2:B5",
                    "internal": {},
                    "exclude_rows": [4],
                },
            ]
        },
    )
    findings = find_duplicate_internal_formula_cell_bindings(
        cast(DependencyGraph, _FakeGraph()),
        bindings,
        workbook_path=Path("unused.xlsx"),
    )
    assert findings == []


def test_binding_resolution_audit_reports_duplicate_internal_cell_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        (bindings_path / name).write_text(
            (fixture_bindings / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    internals_yaml = bindings_path / "internals.bindings.yaml"
    internals_text = internals_yaml.read_text(encoding="utf-8")
    internals_yaml.write_text(
        internals_text
        + """
  - id: engine_b2_duplicate
    sheet: Engine
    data_range: Engine!B2
    layout: scalar
    internal: {}
    structure:
      measure:
        concept: OBS_VALUE
        dtype: float
        bind:
          kind: data_cell
          read: float
      dimensions: []
    key: []
""",
        encoding="utf-8",
    )

    config = replace(
        synthetic_pipeline_config(workbook_path=workbook_path),
        bindings_path=bindings_path,
    )
    cache_dir = tmp_path / "dependency-graph"
    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)
    regenerate_graph_cache(force=True)
    graph, _ = load_pipeline_dependency_graph(config)
    bindings = load_series_bindings(bindings_path)

    findings = find_duplicate_internal_formula_cell_bindings(
        graph,
        bindings,
        workbook_path=workbook_path,
    )
    assert findings
    assert findings[0].code == "duplicate_internal_cell_binding"
    assert findings[0].address == "Engine!B2"
    assert "engine_b2" in findings[0].message
    assert "engine_b2_duplicate" in findings[0].message

    report = audit_binding_resolutions(
        graph,
        bindings,
        workbook=config.workbook_path,
        directions=("internal",),
    )
    assert not report.ok
    assert any(
        finding.code == "duplicate_internal_cell_binding" for finding in report.findings
    )


def test_binding_resolution_audit_clean_for_synthetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    config = synthetic_pipeline_config(workbook_path=workbook_path)
    cache_dir = tmp_path / "dependency-graph"
    _monkeypatch_temp_graph_cache(monkeypatch, cache_dir=cache_dir, config=config)
    regenerate_graph_cache(force=True)
    graph, _ = load_pipeline_dependency_graph(config)
    bindings = load_series_bindings(config.bindings_path)

    report = audit_binding_resolutions(
        graph,
        bindings,
        workbook=config.workbook_path,
    )
    assert report.ok
    assert report.error_count == 0


def test_binding_resolution_audit_cli_exits_nonzero_on_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.binding_resolution_audit import main as audit_main

    _prepare_sparse_years_audit_fixture(tmp_path, monkeypatch, patch_audit_cli=True)
    assert audit_main(["--direction", "internal"]) == 1
