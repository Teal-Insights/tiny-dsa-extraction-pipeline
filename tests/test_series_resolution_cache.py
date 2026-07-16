from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from excel_grapher.grapher import DynamicRefConfig
from excel_grapher.series_bindings import (
    derive_input_series,
    derive_internal_series,
    derive_output_series,
)

from src.graph_cache import get_or_build_dependency_graph
from src.series_resolution_cache import (
    DEFAULT_SERIES_RESOLUTION_CACHE_DIR,
    clear_series_resolution_cache,
    get_or_build_series_resolution,
    load_series_resolution_payload,
    series_resolution_cache_key,
)
from tests.fixtures.synthetic_pipeline import (
    synthetic_pipeline_config,
    write_synthetic_workbook,
)
from tests.fixtures.test_state import REPO_SERIES_RESOLUTION_CACHE_DIR


@pytest.fixture
def graph_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "dependency-graph"


@pytest.fixture
def series_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "series-resolution"


@pytest.fixture
def synthetic_config(tmp_path: Path):
    workbook_path = tmp_path / "workbook.xlsx"
    write_synthetic_workbook(workbook_path)
    return synthetic_pipeline_config(workbook_path=workbook_path)


def _build_graph(config, *, cache_dir: Path):
    dynamic_refs = DynamicRefConfig.from_constraints(config.constraints, {})
    return get_or_build_dependency_graph(
        workbook_path=config.workbook_path,
        targets=config.targets,
        constraints=config.constraints,
        bindings_path=config.bindings_path,
        dynamic_refs=dynamic_refs,
        cache_dir=cache_dir,
    )


def _load_bindings(config):
    from excel_grapher.series_bindings import load_series_bindings

    return load_series_bindings(config.bindings_path)


def test_pytest_uses_isolated_series_resolution_disk_cache() -> None:
    assert DEFAULT_SERIES_RESOLUTION_CACHE_DIR != REPO_SERIES_RESOLUTION_CACHE_DIR


def test_series_resolution_cache_roundtrip(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)

    first = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    second = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )

    assert not first.cache_hit
    assert second.cache_hit
    assert first.cache_key == second.cache_key
    assert first.input_series == second.input_series
    assert first.output_series == second.output_series
    assert first.internal_series == second.internal_series
    assert first.input_series
    assert first.output_series


def test_series_resolution_cache_matches_live_derive(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)
    cached = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    assert cached.input_series == derive_input_series(
        graph_result.graph, bindings, workbook=synthetic_config.workbook_path
    )
    assert cached.output_series == derive_output_series(
        graph_result.graph, bindings, workbook=synthetic_config.workbook_path
    )
    assert cached.internal_series == derive_internal_series(
        graph_result.graph, bindings, workbook=synthetic_config.workbook_path
    )


def test_series_resolution_cache_force_rebuild(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)
    first = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    second = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
        force_rebuild=True,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key == second.cache_key


def test_series_resolution_cache_no_cache_bypasses_disk(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)
    result = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
        no_cache=True,
    )

    assert not result.cache_hit
    assert (
        load_series_resolution_payload(result.cache_key, cache_dir=series_cache_dir)
        is None
    )


def test_series_resolution_cache_key_follows_graph_cache_key() -> None:
    first = series_resolution_cache_key(graph_cache_key="abc")
    second = series_resolution_cache_key(graph_cache_key="def")
    assert first != second


def test_series_resolution_cache_miss_when_graph_cache_key_changes(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)
    first = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    other_graph_key = hashlib.sha256(b"other-graph-key").hexdigest()
    second = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=other_graph_key,
        cache_dir=series_cache_dir,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_corrupt_series_resolution_cache_is_rebuilt(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)
    first = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    payload_path = series_cache_dir / f"{first.cache_key}.pkl.gz"
    payload_path.write_bytes(b"not-a-valid-gzip-pickle")

    second = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    assert not second.cache_hit
    assert payload_path.is_file()


def test_clear_series_resolution_cache_removes_entries(
    synthetic_config,
    graph_cache_dir: Path,
    series_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    bindings = _load_bindings(synthetic_config)
    result = get_or_build_series_resolution(
        graph_result.graph,
        bindings,
        workbook_path=synthetic_config.workbook_path,
        graph_cache_key=graph_result.cache_key,
        cache_dir=series_cache_dir,
    )
    payload_path = series_cache_dir / f"{result.cache_key}.pkl.gz"
    assert payload_path.is_file()
    clear_series_resolution_cache(cache_dir=series_cache_dir)
    assert not payload_path.is_file()


def test_build_pipeline_graph_uses_series_resolution_cache(
    synthetic_config,
    series_cache_dir: Path,
) -> None:
    from src.extraction_pipeline import build_pipeline_graph
    import src.series_resolution_cache as series_resolution_cache

    with patch.object(
        series_resolution_cache,
        "DEFAULT_SERIES_RESOLUTION_CACHE_DIR",
        series_cache_dir,
    ):
        first = build_pipeline_graph(synthetic_config)
        second = build_pipeline_graph(synthetic_config)

    assert first.input_series == second.input_series
    assert first.output_series == second.output_series
    assert first.internal_series == second.internal_series
    assert list(series_cache_dir.glob("*.pkl.gz"))


def test_build_pipeline_graph_no_cache_bypasses_series_resolution_cache(
    synthetic_config,
    series_cache_dir: Path,
) -> None:
    from src.extraction_pipeline import build_pipeline_graph
    import src.series_resolution_cache as series_resolution_cache

    with patch.object(
        series_resolution_cache,
        "DEFAULT_SERIES_RESOLUTION_CACHE_DIR",
        series_cache_dir,
    ):
        build_pipeline_graph(synthetic_config, no_cache=True)

    assert not list(series_cache_dir.glob("*.pkl.gz"))
