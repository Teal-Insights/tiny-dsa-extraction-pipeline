from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import pytest
from excel_grapher.grapher import DynamicRefConfig
from excel_grapher.series_bindings import load_series_bindings

from src.dependency_graph_viz import series_cell_keys
from src.extraction_pipeline import classify_leaves_from_constraints
from src.graph_cache import get_or_build_dependency_graph
from src.internal_binding_coverage import enforce_internal_binding_coverage
from src.internal_bindings import (
    build_address_to_series_id,
    build_bound_address_keys,
    build_internal_binding_index,
)
from src.series_derived_cache import (
    DEFAULT_SERIES_DERIVED_CACHE_DIR,
    clear_series_derived_cache,
    get_or_build_series_derived,
    load_series_derived_payload,
    prune_stale_series_derived_cache_entries,
    require_bound_address_keys_from_series_derived_cache,
    series_derived_cache_key,
)
from src.series_resolution_cache import get_or_build_series_resolution
from tests.fixtures.synthetic_pipeline import (
    synthetic_pipeline_config,
    write_synthetic_workbook,
)
from tests.fixtures.test_state import REPO_SERIES_DERIVED_CACHE_DIR


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
def derived_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "series-derived"


@pytest.fixture
def synthetic_config(tmp_path: Path):
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    return synthetic_pipeline_config(workbook_path=workbook_path)


def _build_graph(config, *, cache_dir: Path):
    dynamic_refs = DynamicRefConfig.from_constraints(config.constraints)
    return get_or_build_dependency_graph(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        dynamic_refs=dynamic_refs,
        cache_dir=cache_dir,
    )


def _resolve_series(config, graph_result):
    bindings = load_series_bindings(config.bindings_path)
    return get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        bindings_path=config.bindings_path,
        cache_dir=None,
        no_cache=True,
    )


def _get_or_build_derived(
    config,
    graph_result,
    series_result,
    *,
    cache_dir: Path,
    no_cache: bool = False,
    force_rebuild: bool = False,
    graph_cache_key: str | None = None,
    validation_mode=None,
    exempt_cells=None,
):
    mode = (
        validation_mode
        if validation_mode is not None
        else config.internal_binding_validation_mode
    )
    exempt = (
        exempt_cells
        if exempt_cells is not None
        else config.internal_binding_exempt_cells
    )
    return get_or_build_series_derived(
        graph_result.graph,
        constraints=config.constraints,
        input_series=series_result.input_series,
        output_series=series_result.output_series,
        internal_series=series_result.internal_series,
        constant_series=series_result.constant_series,
        input_cells=series_cell_keys(series_result.input_series),
        output_cells=series_cell_keys(series_result.output_series),
        exempt_cells=exempt,
        validation_mode=mode,
        context="pipeline",
        graph_cache_key=(
            graph_cache_key if graph_cache_key is not None else graph_result.cache_key
        ),
        bindings_path=config.bindings_path,
        cache_dir=cache_dir,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )


def test_pytest_uses_isolated_series_derived_disk_cache() -> None:
    assert DEFAULT_SERIES_DERIVED_CACHE_DIR != REPO_SERIES_DERIVED_CACHE_DIR


def test_series_derived_cache_roundtrip(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)

    first = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    second = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )

    assert not first.cache_hit
    assert second.cache_hit
    assert first.cache_key == second.cache_key
    assert first.leaf_classification == second.leaf_classification
    assert first.bound_address_keys == second.bound_address_keys
    assert first.address_to_series_id == second.address_to_series_id
    assert first.internal_binding_index == second.internal_binding_index
    assert first.coverage_report == second.coverage_report
    assert first.leaf_classification
    assert first.bound_address_keys


def test_series_derived_cache_matches_live_builders(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    cached = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )

    expected_leaves = classify_leaves_from_constraints(
        synthetic_config.constraints, graph_result.graph.leaf_keys()
    )
    expected_index = build_internal_binding_index(series_result.internal_series)
    expected_bound = build_bound_address_keys(
        series_result.input_series,
        series_result.output_series,
        series_result.internal_series,
        constant_series=series_result.constant_series,
    )
    expected_address_map = build_address_to_series_id(
        series_result.internal_series,
        output_series=series_result.output_series,
        input_series=series_result.input_series,
        constant_series=series_result.constant_series,
    )
    expected_coverage = enforce_internal_binding_coverage(
        graph=graph_result.graph,
        internal_series=series_result.internal_series,
        input_cells=series_cell_keys(series_result.input_series),
        output_cells=series_cell_keys(series_result.output_series),
        exempt_cells=synthetic_config.internal_binding_exempt_cells,
        mode=synthetic_config.internal_binding_validation_mode,
        context="pipeline",
    )

    assert cached.leaf_classification == expected_leaves
    assert cached.internal_binding_index == expected_index
    assert cached.bound_address_keys == expected_bound
    assert cached.address_to_series_id == expected_address_map
    assert cached.coverage_report == expected_coverage


def test_series_derived_cache_force_rebuild(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    first = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    second = _get_or_build_derived(
        synthetic_config,
        graph_result,
        series_result,
        cache_dir=derived_cache_dir,
        force_rebuild=True,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key == second.cache_key


def test_series_derived_cache_no_cache_bypasses_disk(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    result = _get_or_build_derived(
        synthetic_config,
        graph_result,
        series_result,
        cache_dir=derived_cache_dir,
        no_cache=True,
    )

    assert not result.cache_hit
    assert (
        load_series_derived_payload(result.cache_key, cache_dir=derived_cache_dir)
        is None
    )


def test_series_derived_cache_key_follows_graph_cache_key(
    synthetic_config,
) -> None:
    first = series_derived_cache_key(
        graph_cache_key="abc",
        bindings_path=synthetic_config.bindings_path,
        validation_mode="warn",
        exempt_cells=frozenset(),
    )
    second = series_derived_cache_key(
        graph_cache_key="def",
        bindings_path=synthetic_config.bindings_path,
        validation_mode="warn",
        exempt_cells=frozenset(),
    )
    assert first != second


def test_series_derived_cache_key_follows_bindings_fingerprint(
    synthetic_config,
    tmp_path: Path,
) -> None:
    alternate_bindings = tmp_path / "bindings"
    alternate_bindings.mkdir()
    for binding_file in synthetic_config.bindings_path.glob("*.bindings.yaml"):
        content = binding_file.read_text(encoding="utf-8") + "\n# cache-bust\n"
        (alternate_bindings / binding_file.name).write_text(content, encoding="utf-8")

    first = series_derived_cache_key(
        graph_cache_key="same-graph-key",
        bindings_path=synthetic_config.bindings_path,
        validation_mode="warn",
        exempt_cells=frozenset(),
    )
    second = series_derived_cache_key(
        graph_cache_key="same-graph-key",
        bindings_path=alternate_bindings,
        validation_mode="warn",
        exempt_cells=frozenset(),
    )
    assert first != second


def test_series_derived_cache_key_follows_mode_and_exemptions(
    synthetic_config,
) -> None:
    base = series_derived_cache_key(
        graph_cache_key="abc",
        bindings_path=synthetic_config.bindings_path,
        validation_mode="warn",
        exempt_cells=frozenset(),
    )
    mode_changed = series_derived_cache_key(
        graph_cache_key="abc",
        bindings_path=synthetic_config.bindings_path,
        validation_mode="error",
        exempt_cells=frozenset(),
    )
    exempt_changed = series_derived_cache_key(
        graph_cache_key="abc",
        bindings_path=synthetic_config.bindings_path,
        validation_mode="warn",
        exempt_cells=frozenset({"Sheet!A1"}),
    )
    assert base != mode_changed
    assert base != exempt_changed


def test_series_derived_cache_miss_when_graph_cache_key_changes(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    first = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    other_graph_key = hashlib.sha256(b"other-graph-key").hexdigest()
    second = _get_or_build_derived(
        synthetic_config,
        graph_result,
        series_result,
        cache_dir=derived_cache_dir,
        graph_cache_key=other_graph_key,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_corrupt_series_derived_cache_is_rebuilt(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    first = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    payload_path = derived_cache_dir / f"{first.cache_key}.pkl.gz"
    payload_path.write_bytes(b"not-a-valid-gzip-pickle")

    second = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    assert not second.cache_hit
    assert payload_path.is_file()


def test_legacy_short_tuple_series_derived_cache_is_rebuilt(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    import gzip
    import pickle

    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    cache_key = series_derived_cache_key(
        graph_cache_key=graph_result.cache_key,
        bindings_path=synthetic_config.bindings_path,
        validation_mode=synthetic_config.internal_binding_validation_mode,
        exempt_cells=synthetic_config.internal_binding_exempt_cells,
    )
    derived_cache_dir.mkdir(parents=True, exist_ok=True)
    payload_path = derived_cache_dir / f"{cache_key}.pkl.gz"
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(({}, {}), handle, protocol=pickle.HIGHEST_PROTOCOL)

    result = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    assert not result.cache_hit
    loaded = load_series_derived_payload(cache_key, cache_dir=derived_cache_dir)
    assert loaded is not None
    assert len(loaded) == 5


def test_clear_series_derived_cache_removes_entries(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    result = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    payload_path = derived_cache_dir / f"{result.cache_key}.pkl.gz"
    assert payload_path.is_file()
    clear_series_derived_cache(cache_dir=derived_cache_dir)
    assert not payload_path.is_file()


def test_prune_stale_series_derived_cache_entries_removes_only_stale_keys(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "series-derived"
    cache_dir.mkdir()
    keep = cache_dir / "keep.pkl.gz"
    drop = cache_dir / "drop.pkl.gz"
    keep.write_bytes(b"keep")
    drop.write_bytes(b"drop")

    pruned = prune_stale_series_derived_cache_entries(
        {"keep"},
        cache_dir=cache_dir,
    )

    assert pruned == ["drop.pkl.gz"]
    assert keep.is_file()
    assert not drop.is_file()


def test_get_or_build_series_derived_prunes_other_excel_grapher_versions(
    synthetic_config,
    graph_cache_dir: Path,
    derived_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    series_result = _resolve_series(synthetic_config, graph_result)
    first = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )
    _write_versioned_cache_pair(
        derived_cache_dir, "stale-old-version", excel_grapher_version="0.0.1"
    )
    _write_versioned_cache_pair(
        derived_cache_dir,
        "sibling-current-version",
        excel_grapher_version=version("excel-grapher"),
    )

    second = _get_or_build_derived(
        synthetic_config, graph_result, series_result, cache_dir=derived_cache_dir
    )

    assert second.cache_hit
    assert second.cache_key == first.cache_key
    assert (derived_cache_dir / f"{first.cache_key}.pkl.gz").is_file()
    assert (derived_cache_dir / "sibling-current-version.pkl.gz").is_file()
    assert not (derived_cache_dir / "stale-old-version.pkl.gz").is_file()
    assert not (derived_cache_dir / "stale-old-version.meta.json").is_file()


def test_build_pipeline_graph_uses_series_derived_cache(
    synthetic_config,
    derived_cache_dir: Path,
) -> None:
    from src import series_derived_cache
    from src.extraction_pipeline import build_pipeline_graph

    with patch.object(
        series_derived_cache,
        "DEFAULT_SERIES_DERIVED_CACHE_DIR",
        derived_cache_dir,
    ):
        first = build_pipeline_graph(synthetic_config)
        second = build_pipeline_graph(synthetic_config)

    assert first.leaf_classification == second.leaf_classification
    assert first.bound_address_keys == second.bound_address_keys
    assert first.address_to_series_id == second.address_to_series_id
    assert first.internal_binding_index == second.internal_binding_index
    assert getattr(first.graph, "leaf_classification", None) in (None, {})
    assert list(derived_cache_dir.glob("*.pkl.gz"))


def test_build_pipeline_graph_no_cache_bypasses_series_derived_cache(
    synthetic_config,
    derived_cache_dir: Path,
) -> None:
    from src import series_derived_cache
    from src.extraction_pipeline import build_pipeline_graph

    with patch.object(
        series_derived_cache,
        "DEFAULT_SERIES_DERIVED_CACHE_DIR",
        derived_cache_dir,
    ):
        build_pipeline_graph(synthetic_config, no_cache=True)

    assert not list(derived_cache_dir.glob("*.pkl.gz"))


def test_build_pipeline_graph_does_not_mutate_shared_leaf_classification(
    synthetic_config,
    derived_cache_dir: Path,
) -> None:
    from src import series_derived_cache
    from src.extraction_pipeline import build_pipeline_graph

    with patch.object(
        series_derived_cache,
        "DEFAULT_SERIES_DERIVED_CACHE_DIR",
        derived_cache_dir,
    ):
        result = build_pipeline_graph(synthetic_config)

    assert result.leaf_classification
    assert getattr(result.graph, "leaf_classification", None) in (None, {})


def test_require_bound_address_keys_fails_loudly_on_cache_miss(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="series-derived cache miss"):
        require_bound_address_keys_from_series_derived_cache(
            graph_cache_key="missing",
            bindings_path=tmp_path / "bindings",
            validation_mode="warn",
            exempt_cells=frozenset(),
            cache_dir=tmp_path / "series-derived",
        )


def test_call_sites_read_derived_fields_instead_of_rebuilding(
    synthetic_config,
    derived_cache_dir: Path,
) -> None:
    """Warm pipeline result carries derived fields; builders are not needed again."""
    from src import series_derived_cache
    from src.extraction_pipeline import build_pipeline_graph

    with patch.object(
        series_derived_cache,
        "DEFAULT_SERIES_DERIVED_CACHE_DIR",
        derived_cache_dir,
    ):
        result = build_pipeline_graph(synthetic_config)

    with (
        patch(
            "src.internal_bindings.build_bound_address_keys",
            side_effect=AssertionError("rebuild"),
        ),
        patch(
            "src.internal_bindings.build_address_to_series_id",
            side_effect=AssertionError("rebuild"),
        ),
        patch(
            "src.internal_bindings.build_internal_binding_index",
            side_effect=AssertionError("rebuild"),
        ),
        patch(
            "src.extraction_pipeline.classify_leaves_from_constraints",
            side_effect=AssertionError("rebuild"),
        ),
    ):
        assert result.bound_address_keys
        assert result.address_to_series_id
        assert result.internal_binding_index
        assert result.leaf_classification
