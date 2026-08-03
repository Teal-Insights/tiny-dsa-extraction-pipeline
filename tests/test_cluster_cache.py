from __future__ import annotations

import gzip
import hashlib
import json
import pickle
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import pytest

from src.cluster_cache import (
    DEFAULT_CLUSTER_CACHE_DIR,
    clear_cluster_cache,
    cluster_cache_key,
    get_or_build_clusters_and_schedule,
    load_cluster_payload,
    prune_stale_cluster_cache_entries,
)
from src.formula_clustering import FormulaCluster, cluster_graph_formulas
from src.graph_cache import bindings_fingerprint
from src.projection_cache import projection_cache_key
from src.refactor_order import RefactorUnit, compute_refactor_schedule
from src.subgraph_projection import build_refactor_projection
from tests.fixtures.synthetic_pipeline import (
    synthetic_pipeline_config,
    write_synthetic_workbook,
)
from tests.fixtures.test_state import REPO_CLUSTER_CACHE_DIR


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


@pytest.fixture
def graph_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "dependency-graph"


@pytest.fixture
def cluster_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "clusters"


@pytest.fixture
def synthetic_config(tmp_path: Path):
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    return synthetic_pipeline_config(workbook_path=workbook_path)


def _projection_and_bindings(config, *, graph_cache_dir: Path):
    from src.extraction_pipeline import build_pipeline_graph

    del graph_cache_dir
    graph_result = build_pipeline_graph(config)
    projection = build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.graph_cache_key,
    )
    return graph_result, projection


def _call_get_or_build(
    config,
    *,
    graph_result,
    projection,
    cluster_cache_dir: Path,
    variation_mode: str | None = None,
    clustering_mode: str | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
    projection_key: str | None = None,
    bindings_path: Path | None = None,
):
    return get_or_build_clusters_and_schedule(
        projection,
        bound_address_keys=graph_result.bound_address_keys,
        address_to_series_id=graph_result.address_to_series_id,
        workbook_path=config.workbook_path,
        layout=config.projection_layout,
        bindings_path=bindings_path or config.bindings_path,
        projection_cache_key=projection_key
        or projection_cache_key(graph_cache_key=graph_result.graph_cache_key),
        variation_mode=variation_mode or config.variation_mode,
        clustering_mode=clustering_mode or config.clustering_mode,
        cache_dir=cluster_cache_dir,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )


def test_pytest_uses_isolated_cluster_disk_cache() -> None:
    assert DEFAULT_CLUSTER_CACHE_DIR != REPO_CLUSTER_CACHE_DIR


def test_cluster_cache_roundtrip(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )

    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )

    assert not first.cache_hit
    assert second.cache_hit
    assert first.cache_key == second.cache_key
    assert first.clusters == second.clusters
    assert first.schedule == second.schedule
    assert first.clusters
    assert first.schedule


def test_cluster_cache_matches_live_cluster_and_schedule(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    cached = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    live_clusters = cluster_graph_formulas(
        projection,
        bound_address_keys=graph_result.bound_address_keys,
        variation_mode=synthetic_config.variation_mode,
        clustering_mode=synthetic_config.clustering_mode,
        address_to_series_id=graph_result.address_to_series_id,
        workbook_path=synthetic_config.workbook_path,
        layout=synthetic_config.projection_layout,
    )
    live_schedule = compute_refactor_schedule(projection, live_clusters)
    assert cached.clusters == live_clusters
    assert cached.schedule == live_schedule


def test_cluster_cache_force_rebuild(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        force_rebuild=True,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key == second.cache_key


def test_cluster_cache_no_cache_bypasses_disk(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    result = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        no_cache=True,
    )

    assert not result.cache_hit
    assert load_cluster_payload(result.cache_key, cache_dir=cluster_cache_dir) is None


def test_cluster_cache_key_follows_projection_cache_key(
    synthetic_config,
) -> None:
    bindings_fp = bindings_fingerprint(synthetic_config.bindings_path)
    first = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
    )
    second = cluster_cache_key(
        projection_cache_key="def",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
    )
    assert first != second


def test_cluster_cache_key_miss_when_variation_mode_changes(
    synthetic_config,
) -> None:
    bindings_fp = bindings_fingerprint(synthetic_config.bindings_path)
    first = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
    )
    second = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="dominant_key_only",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
    )
    assert first != second


def test_cluster_cache_key_miss_when_clustering_mode_changes(
    synthetic_config,
) -> None:
    bindings_fp = bindings_fingerprint(synthetic_config.bindings_path)
    first = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
    )
    second = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="ast",
        bindings_fingerprint=bindings_fp,
    )
    assert first != second


def test_cluster_cache_key_miss_when_projection_layout_changes(
    synthetic_config,
) -> None:
    """PROJECTION_LAYOUT must participate in the clusters cache key (#234 review)."""
    from dataclasses import replace

    from src.workbook_addresses import ProjectionColumnLayout

    bindings_fp = bindings_fingerprint(synthetic_config.bindings_path)
    layout_a = ProjectionColumnLayout(
        engine_sheet="Engine",
        engine_columns=("B", "C"),
        outputs_sheet="Outputs",
        outputs_column_to_engine={"B": "B", "C": "C"},
        time_period_to_engine_column={1: "B", 2: "C"},
    )
    layout_b = replace(
        layout_a,
        engine_columns=("B", "C", "D"),
        time_period_to_engine_column={1: "B", 2: "C", 3: "D"},
    )
    first = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
        projection_layout=layout_a,
    )
    second = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
        projection_layout=layout_b,
    )
    assert first != second


def test_cluster_cache_key_miss_when_projection_layout_cleared(
    synthetic_config,
) -> None:
    """``None`` layout must not collide with a concrete PROJECTION_LAYOUT."""
    bindings_fp = bindings_fingerprint(synthetic_config.bindings_path)
    layout = synthetic_config.projection_layout
    assert layout is not None
    with_layout = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
        projection_layout=layout,
    )
    without_layout = cluster_cache_key(
        projection_cache_key="abc",
        variation_mode="independent",
        clustering_mode="series_ast",
        bindings_fingerprint=bindings_fp,
        projection_layout=None,
    )
    assert with_layout != without_layout


def test_cluster_cache_miss_when_projection_layout_changes(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    from dataclasses import replace

    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    altered = replace(
        synthetic_config,
        projection_layout=replace(
            synthetic_config.projection_layout,
            engine_columns=("B", "C", "D"),
            time_period_to_engine_column={1: "B", 2: "C", 3: "D"},
        ),
    )
    second = _call_get_or_build(
        altered,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_cluster_cache_miss_when_variation_mode_changes(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        variation_mode="independent",
    )
    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        variation_mode="dominant_key_only",
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_cluster_cache_miss_when_clustering_mode_changes(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        clustering_mode="series_ast",
    )
    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        clustering_mode="ast",
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_cluster_cache_miss_when_projection_cache_key_changes(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    other_projection_key = hashlib.sha256(b"other-projection-key").hexdigest()
    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        projection_key=other_projection_key,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_cluster_cache_miss_when_bindings_fingerprint_changes(
    synthetic_config,
    cluster_cache_dir: Path,
    tmp_path: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )

    other_bindings = tmp_path / "other-bindings"
    other_bindings.mkdir()
    for path in synthetic_config.bindings_path.glob("*.bindings.yaml"):
        (other_bindings / path.name).write_text(
            path.read_text(encoding="utf-8") + "\n# fingerprint-change\n",
            encoding="utf-8",
        )
    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
        bindings_path=other_bindings,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_corrupt_cluster_cache_is_rebuilt(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    payload_path = cluster_cache_dir / f"{first.cache_key}.pkl.gz"
    payload_path.write_bytes(b"not-a-valid-gzip-pickle")

    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    assert not second.cache_hit
    assert payload_path.is_file()


def test_invalid_cluster_payload_shape_is_rebuilt(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    cache_key = cluster_cache_key(
        projection_cache_key=projection_cache_key(
            graph_cache_key=graph_result.graph_cache_key
        ),
        variation_mode=synthetic_config.variation_mode,
        clustering_mode=synthetic_config.clustering_mode,
        bindings_fingerprint=bindings_fingerprint(synthetic_config.bindings_path),
    )
    cluster_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path = cluster_cache_dir / f"{cache_key}.pkl.gz"
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(([], []), handle, protocol=pickle.HIGHEST_PROTOCOL)

    result = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    assert not result.cache_hit
    assert all(isinstance(cluster, FormulaCluster) for cluster in result.clusters)
    assert all(isinstance(unit, RefactorUnit) for unit in result.schedule)


def test_clear_cluster_cache_removes_entries(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    result = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    payload_path = cluster_cache_dir / f"{result.cache_key}.pkl.gz"
    assert payload_path.is_file()
    clear_cluster_cache(cache_dir=cluster_cache_dir)
    assert not payload_path.is_file()


def test_prune_stale_cluster_cache_entries_removes_only_stale_keys(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "clusters"
    cache_dir.mkdir()
    keep = cache_dir / "keep.pkl.gz"
    drop = cache_dir / "drop.pkl.gz"
    keep.write_bytes(b"keep")
    drop.write_bytes(b"drop")

    pruned = prune_stale_cluster_cache_entries(
        {"keep"},
        cache_dir=cache_dir,
    )

    assert pruned == ["drop.pkl.gz"]
    assert keep.is_file()
    assert not drop.is_file()


def test_get_or_build_clusters_prunes_other_excel_grapher_versions(
    synthetic_config,
    cluster_cache_dir: Path,
) -> None:
    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    first = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )
    _write_versioned_cache_pair(
        cluster_cache_dir, "stale-old-version", excel_grapher_version="0.0.1"
    )
    _write_versioned_cache_pair(
        cluster_cache_dir,
        "sibling-current-version",
        excel_grapher_version=version("excel-grapher"),
    )

    second = _call_get_or_build(
        synthetic_config,
        graph_result=graph_result,
        projection=projection,
        cluster_cache_dir=cluster_cache_dir,
    )

    assert second.cache_hit
    assert second.cache_key == first.cache_key
    assert (cluster_cache_dir / f"{first.cache_key}.pkl.gz").is_file()
    assert (cluster_cache_dir / "sibling-current-version.pkl.gz").is_file()
    assert not (cluster_cache_dir / "stale-old-version.pkl.gz").is_file()
    assert not (cluster_cache_dir / "stale-old-version.meta.json").is_file()


def test_run_refactor_stage_uses_cluster_cache(
    synthetic_config,
    cluster_cache_dir: Path,
    tmp_path: Path,
) -> None:
    from src import cluster_cache
    from src.extraction_pipeline import ExportStageState, run_refactor_stage

    graph_result, _projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "internals.py").write_text(
        "def placeholder():\n    return None\n",
        encoding="utf-8",
    )
    from src.series_derived_cache import series_derived_cache_key

    proj_key = projection_cache_key(graph_cache_key=graph_result.graph_cache_key)
    derived_key = series_derived_cache_key(
        graph_cache_key=graph_result.graph_cache_key,
        validation_mode=synthetic_config.internal_binding_validation_mode,
        exempt_cells=synthetic_config.internal_binding_exempt_cells,
    )
    state = ExportStageState(
        config=synthetic_config,
        graph_cache_key=graph_result.graph_cache_key,
        projection_cache_key=proj_key,
        series_derived_cache_key=derived_key,
        codegen_cache_key="codegen-key",
        package_root=package_root,
    )

    with (
        patch.object(
            cluster_cache,
            "DEFAULT_CLUSTER_CACHE_DIR",
            cluster_cache_dir,
        ),
        patch(
            "src.internals_refactor.refactor_internals_all_clusters",
            return_value=(),
        ),
    ):
        run_refactor_stage(state)
        run_refactor_stage(state)

    assert list(cluster_cache_dir.glob("*.pkl.gz"))


def test_run_refactor_stage_no_cache_bypasses_cluster_cache(
    synthetic_config,
    cluster_cache_dir: Path,
    tmp_path: Path,
) -> None:
    from src import cluster_cache
    from src.extraction_pipeline import ExportStageState, run_refactor_stage
    from src.series_derived_cache import series_derived_cache_key

    graph_result, projection = _projection_and_bindings(
        synthetic_config, graph_cache_dir=Path()
    )
    del projection
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "internals.py").write_text(
        "def placeholder():\n    return None\n",
        encoding="utf-8",
    )
    proj_key = projection_cache_key(graph_cache_key=graph_result.graph_cache_key)
    derived_key = series_derived_cache_key(
        graph_cache_key=graph_result.graph_cache_key,
        validation_mode=synthetic_config.internal_binding_validation_mode,
        exempt_cells=synthetic_config.internal_binding_exempt_cells,
    )
    state = ExportStageState(
        config=synthetic_config,
        graph_cache_key=graph_result.graph_cache_key,
        projection_cache_key=proj_key,
        series_derived_cache_key=derived_key,
        codegen_cache_key="codegen-key",
        package_root=package_root,
    )

    with (
        patch.object(
            cluster_cache,
            "DEFAULT_CLUSTER_CACHE_DIR",
            cluster_cache_dir,
        ),
        patch(
            "src.internals_refactor.refactor_internals_all_clusters",
            return_value=(),
        ),
    ):
        run_refactor_stage(state, no_cache=True)

    assert not list(cluster_cache_dir.glob("*.pkl.gz"))
