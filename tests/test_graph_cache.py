from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, cast
from unittest.mock import ANY, patch

import pytest
from excel_grapher.core.cell_types import RealBetween
from excel_grapher.exporter import CodeGenerator
from excel_grapher.exporter.codegen import GraphLike
from excel_grapher.grapher import DynamicRefConfig

from src.bindings_validation_cache import DEFAULT_BINDINGS_VALIDATION_CACHE_DIR
from src.cluster_cache import DEFAULT_CLUSTER_CACHE_DIR
from src.codegen_cache import DEFAULT_CODEGEN_CACHE_DIR
from src.graph_cache import (
    DEFAULT_GRAPH_CACHE_DIR,
    bindings_fingerprint,
    clear_dependency_graph_cache,
    clear_process_dependency_graph_cache,
    dependency_graph_cache_key,
    get_or_build_dependency_graph,
    load_dependency_graph,
    prune_cache_entries_for_other_excel_grapher_versions,
)
from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR
from src.projection_cache import (
    DEFAULT_PROJECTION_CACHE_DIR,
    clear_projection_cache,
    get_or_build_refactor_projection,
    projection_cache_key,
    rehydrate_projection_result,
)
from src.series_resolution_cache import DEFAULT_SERIES_RESOLUTION_CACHE_DIR
from src.subgraph_projection import build_refactor_projection
from tests.fixtures.synthetic_pipeline import (
    CONSTRAINTS,
    synthetic_pipeline_config,
    write_synthetic_workbook,
)
from tests.fixtures.test_state import (
    REPO_BINDINGS_VALIDATION_CACHE_DIR,
    REPO_CLUSTER_CACHE_DIR,
    REPO_CODEGEN_CACHE_DIR,
    REPO_GRAPH_CACHE_DIR,
    REPO_INTERNALS_CACHE_DIR,
    REPO_PROJECTION_CACHE_DIR,
    REPO_SERIES_RESOLUTION_CACHE_DIR,
)


def _write_versioned_cache_pair(
    cache_dir: Path,
    cache_key: str,
    *,
    excel_grapher_version: str,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{cache_key}.pkl.gz").write_bytes(b"payload")
    (cache_dir / f"{cache_key}.meta.json").write_text(
        json.dumps(
            {
                "cache_key": cache_key,
                "excel_grapher_version": excel_grapher_version,
            }
        ),
        encoding="utf-8",
    )


def test_pytest_uses_isolated_pipeline_disk_cache() -> None:
    assert DEFAULT_GRAPH_CACHE_DIR != REPO_GRAPH_CACHE_DIR
    assert DEFAULT_PROJECTION_CACHE_DIR != REPO_PROJECTION_CACHE_DIR
    assert DEFAULT_SERIES_RESOLUTION_CACHE_DIR != REPO_SERIES_RESOLUTION_CACHE_DIR
    assert DEFAULT_BINDINGS_VALIDATION_CACHE_DIR != REPO_BINDINGS_VALIDATION_CACHE_DIR
    assert DEFAULT_CODEGEN_CACHE_DIR != REPO_CODEGEN_CACHE_DIR
    assert DEFAULT_CLUSTER_CACHE_DIR != REPO_CLUSTER_CACHE_DIR
    assert DEFAULT_INTERNALS_CACHE_DIR != REPO_INTERNALS_CACHE_DIR


@pytest.fixture
def graph_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "dependency-graph"


@pytest.fixture
def projection_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "projection"


@pytest.fixture
def synthetic_config(tmp_path: Path):
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    return synthetic_pipeline_config(workbook_path=workbook_path)


def _build_graph(config, *, cache_dir: Path, **kwargs):
    dynamic_refs = DynamicRefConfig.from_constraints(config.constraints, {})
    return get_or_build_dependency_graph(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        bindings_path=config.bindings_path,
        dynamic_refs=dynamic_refs,
        cache_dir=cache_dir,
        **kwargs,
    )


def test_dependency_graph_cache_roundtrip(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    second = _build_graph(synthetic_config, cache_dir=graph_cache_dir)

    assert not first.cache_hit
    assert second.cache_hit
    assert first.cache_key == second.cache_key
    assert len(first.graph) == len(second.graph)
    assert first.graph.leaf_keys() == second.graph.leaf_keys()


def test_prune_cache_entries_for_other_excel_grapher_versions_keeps_current(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "dependency-graph"
    current = version("excel-grapher")
    _write_versioned_cache_pair(
        cache_dir, "keep-current", excel_grapher_version=current
    )
    _write_versioned_cache_pair(
        cache_dir, "keep-other-current", excel_grapher_version=current
    )
    _write_versioned_cache_pair(cache_dir, "drop-old", excel_grapher_version="0.0.1")

    pruned = prune_cache_entries_for_other_excel_grapher_versions(
        cache_dir=cache_dir,
    )

    assert sorted(pruned) == ["drop-old.meta.json", "drop-old.pkl.gz"]
    assert (cache_dir / "keep-current.pkl.gz").is_file()
    assert (cache_dir / "keep-other-current.pkl.gz").is_file()
    assert not (cache_dir / "drop-old.pkl.gz").is_file()
    assert not (cache_dir / "drop-old.meta.json").is_file()


def test_get_or_build_dependency_graph_prunes_other_excel_grapher_versions(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    _write_versioned_cache_pair(
        graph_cache_dir, "stale-old-version", excel_grapher_version="0.0.1"
    )
    _write_versioned_cache_pair(
        graph_cache_dir,
        "sibling-current-version",
        excel_grapher_version=version("excel-grapher"),
    )

    second = _build_graph(synthetic_config, cache_dir=graph_cache_dir)

    assert second.cache_hit
    assert second.cache_key == first.cache_key
    assert (graph_cache_dir / f"{first.cache_key}.pkl.gz").is_file()
    assert (graph_cache_dir / "sibling-current-version.pkl.gz").is_file()
    assert not (graph_cache_dir / "stale-old-version.pkl.gz").is_file()
    assert not (graph_cache_dir / "stale-old-version.meta.json").is_file()


def test_dependency_graph_cache_force_rebuild(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    second = _build_graph(
        synthetic_config,
        cache_dir=graph_cache_dir,
        force_rebuild=True,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key == second.cache_key


def test_dependency_graph_cache_no_cache_bypasses_disk(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    result = _build_graph(
        synthetic_config,
        cache_dir=graph_cache_dir,
        no_cache=True,
    )

    assert not result.cache_hit
    assert load_dependency_graph(result.cache_key, cache_dir=graph_cache_dir) is None


def test_dependency_graph_process_cache_avoids_second_unpickle(
    synthetic_config,
    graph_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-process lookup for an unchanged key must reuse the held graph.

    Warm pipeline runs previously paid a second ~10–15s gzip unpickle for the
    same ``graph_cache_key`` when a later stage reloaded from disk.
    """
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    assert not first.cache_hit

    load_calls = {"count": 0}
    real_load = load_dependency_graph

    def counting_load(cache_key: str, *, cache_dir: Path | None = None):
        load_calls["count"] += 1
        return real_load(cache_key, cache_dir=cache_dir)

    monkeypatch.setattr(
        "src.graph_cache.load_dependency_graph",
        counting_load,
    )

    second = _build_graph(synthetic_config, cache_dir=graph_cache_dir)

    assert second.cache_hit
    assert second.graph is first.graph
    assert load_calls["count"] == 0


def test_dependency_graph_process_cache_bypassed_by_no_cache(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    second = _build_graph(
        synthetic_config,
        cache_dir=graph_cache_dir,
        no_cache=True,
    )

    assert not second.cache_hit
    assert second.graph is not first.graph


def test_dependency_graph_process_cache_bypassed_by_force_rebuild(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    second = _build_graph(
        synthetic_config,
        cache_dir=graph_cache_dir,
        force_rebuild=True,
    )

    assert not second.cache_hit
    assert second.graph is not first.graph
    third = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    assert third.cache_hit
    assert third.graph is second.graph


def test_dependency_graph_cache_key_changes_when_targets_change(
    synthetic_config,
) -> None:
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        bindings_path=synthetic_config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    changed = replace(synthetic_config, targets=("Outputs!B1",))
    changed_key = dependency_graph_cache_key(
        workbook_path=changed.workbook_path,
        targets=changed.targets,
        constraints=changed.constraints,
        bindings_path=changed.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key != changed_key


def test_dependency_graph_cache_key_changes_when_constraints_change(
    synthetic_config,
) -> None:
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        bindings_path=synthetic_config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    changed_constraints = dict(CONSTRAINTS)
    changed_constraints["Inputs!A1"] = Annotated[float, RealBetween(0.0, 50.0)]
    changed_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=changed_constraints,
        bindings_path=synthetic_config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key != changed_key


def test_dependency_graph_cache_key_changes_when_workbook_changes(
    synthetic_config,
    tmp_path: Path,
) -> None:
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        bindings_path=synthetic_config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    other_workbook = tmp_path / "other-workbook.xlsx"
    write_synthetic_workbook(other_workbook)
    other_workbook.write_bytes(synthetic_config.workbook_path.read_bytes() + b"padding")
    changed_key = dependency_graph_cache_key(
        workbook_path=other_workbook,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        bindings_path=synthetic_config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key != changed_key


def test_dependency_graph_cache_key_changes_when_bindings_change(
    synthetic_config,
    tmp_path: Path,
) -> None:
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        bindings_path=synthetic_config.bindings_path,
        load_values=True,
        capture_dependency_provenance=True,
    )
    alternate_bindings = tmp_path / "bindings"
    alternate_bindings.mkdir()
    for binding_file in synthetic_config.bindings_path.glob("*.bindings.yaml"):
        content = binding_file.read_text(encoding="utf-8")
        if "input_rate" in content:
            content = content.replace("input_rate", "input_rate_alt")
        (alternate_bindings / binding_file.name).write_text(content, encoding="utf-8")

    changed_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        bindings_path=alternate_bindings,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key != changed_key
    assert bindings_fingerprint(synthetic_config.bindings_path) != bindings_fingerprint(
        alternate_bindings
    )


def test_dependency_graph_cache_miss_after_workbook_change(
    synthetic_config,
    graph_cache_dir: Path,
    tmp_path: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)

    other_workbook = tmp_path / "mutated-workbook.xlsx"
    write_synthetic_workbook(other_workbook)
    other_workbook.write_bytes(other_workbook.read_bytes() + b"changed")
    changed_config = replace(synthetic_config, workbook_path=other_workbook)

    second = _build_graph(changed_config, cache_dir=graph_cache_dir)
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_dependency_graph_cache_miss_after_bindings_change(
    synthetic_config,
    graph_cache_dir: Path,
    tmp_path: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)

    alternate_bindings = tmp_path / "bindings"
    alternate_bindings.mkdir()
    for binding_file in synthetic_config.bindings_path.glob("*.bindings.yaml"):
        content = binding_file.read_text(encoding="utf-8") + "\n# cache-bust\n"
        (alternate_bindings / binding_file.name).write_text(content, encoding="utf-8")
    changed_config = replace(synthetic_config, bindings_path=alternate_bindings)

    second = _build_graph(changed_config, cache_dir=graph_cache_dir)
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_corrupt_dependency_graph_cache_is_rebuilt(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    payload_path = graph_cache_dir / f"{first.cache_key}.pkl.gz"
    payload_path.write_bytes(b"not-a-valid-gzip-pickle")
    # Simulate a fresh process that only has the corrupt on-disk entry.
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)

    second = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    assert not second.cache_hit
    assert payload_path.is_file()


def test_projection_cache_roundtrip(
    synthetic_config,
    graph_cache_dir: Path,
    projection_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    first = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        cache_dir=projection_cache_dir,
    )
    second = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        cache_dir=projection_cache_dir,
    )

    assert not first.cache_hit
    assert second.cache_hit
    assert first.cache_key == second.cache_key
    assert len(first.projection) == len(second.projection)


def test_rehydrated_projection_supports_codegen(
    synthetic_config,
    synthetic_series_bindings,
    graph_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    projection = build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
    )
    modules = CodeGenerator(cast(GraphLike, projection)).generate_modules(
        list(graph_result.graph.target_keys()),
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
    )
    assert "def compute_result_a" in modules["api.py"]


def test_projection_cache_key_follows_graph_cache_key() -> None:
    first = projection_cache_key(graph_cache_key="abc")
    second = projection_cache_key(graph_cache_key="def")
    assert first != second


def test_projection_cache_miss_when_graph_cache_key_changes(
    synthetic_config,
    graph_cache_dir: Path,
    projection_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    first = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        cache_dir=projection_cache_dir,
    )
    other_graph_key = hashlib.sha256(b"other-graph-key").hexdigest()
    second = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=other_graph_key,
        cache_dir=projection_cache_dir,
    )
    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_rehydrate_projection_result_uses_original_graph(
    synthetic_graph,
) -> None:
    live = build_refactor_projection(synthetic_graph)
    cached = get_or_build_refactor_projection(
        synthetic_graph,
        graph_cache_key="synthetic-graph-key",
        no_cache=True,
    ).projection
    rehydrated = rehydrate_projection_result(
        original_graph=synthetic_graph,
        projected_graph=cached.projected_graph,
        manifest=cached.manifest,
    )
    assert rehydrated.original_graph is synthetic_graph
    assert len(rehydrated) == len(live)


def test_clear_dependency_graph_cache_removes_entries(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    assert (
        load_dependency_graph(result.cache_key, cache_dir=graph_cache_dir) is not None
    )
    clear_dependency_graph_cache(cache_dir=graph_cache_dir)
    assert load_dependency_graph(result.cache_key, cache_dir=graph_cache_dir) is None


def test_clear_projection_cache_removes_entries(
    synthetic_config,
    graph_cache_dir: Path,
    projection_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    projection_result = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        cache_dir=projection_cache_dir,
    )
    payload_path = projection_cache_dir / f"{projection_result.cache_key}.pkl.gz"
    assert payload_path.is_file()
    clear_projection_cache(cache_dir=projection_cache_dir)
    assert not payload_path.is_file()


def test_extract_graph_cli_supports_no_cache(
    synthetic_config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.extraction_pipeline import main

    monkeypatch.setattr(
        "src.extraction_pipeline.stage_timings_path",
        lambda _repo_root: tmp_path / "stage-timings.json",
    )
    with (
        patch(
            "src.extraction_pipeline.load_pipeline_config",
            return_value=synthetic_config,
        ),
        patch("src.extraction_pipeline.validate_pipeline_config"),
    ):
        with patch("src.extraction_pipeline.extract_dependency_graph") as extract:
            main(["--extract-graph", "--no-cache"])

    extract.assert_called_once_with(
        synthetic_config, no_cache=True, force_rebuild=False, timings=ANY
    )
