from __future__ import annotations

import gzip
import hashlib
import json
import pickle
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Annotated
from unittest.mock import ANY, MagicMock, patch

import pytest
from excel_grapher.core.cell_types import RealBetween
from excel_grapher.exporter import CodeGenerator
from excel_grapher.grapher import DependencyGraph, DynamicRefConfig

from src.bindings_validation_cache import DEFAULT_BINDINGS_VALIDATION_CACHE_DIR
from src.codegen_cache import DEFAULT_CODEGEN_CACHE_DIR
from src.graph_cache import (
    COMMITTED_GRAPH_CACHE_DIR,
    DEFAULT_GRAPH_CACHE_DIR,
    bindings_fingerprint,
    clear_dependency_graph_cache,
    clear_process_dependency_graph_cache,
    dependency_graph_cache_key,
    get_or_build_dependency_graph,
    load_dependency_graph,
    prune_cache_entries_for_other_excel_grapher_versions,
    save_dependency_graph,
    try_load_cached_dependency_graph,
)
from src.projection_cache import (
    DEFAULT_PROJECTION_CACHE_DIR,
    clear_projection_cache,
    get_or_build_refactor_projection,
    projection_cache_key,
    rehydrate_projection_result,
)
from src.series_resolution_cache import DEFAULT_SERIES_RESOLUTION_CACHE_DIR
from tests.fixtures.synthetic_pipeline import (
    CONSTRAINTS,
    synthetic_pipeline_config,
    write_synthetic_workbook,
)
from tests.fixtures.test_state import (
    REPO_BINDINGS_VALIDATION_CACHE_DIR,
    REPO_CODEGEN_CACHE_DIR,
    REPO_GRAPH_CACHE_DIR,
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


def test_committed_graph_cache_dir_stays_on_repo_path_under_pytest_redirect() -> None:
    """Opt-in workbook audits must read the repo cache, not pytest's temp redirect."""
    assert COMMITTED_GRAPH_CACHE_DIR == REPO_GRAPH_CACHE_DIR
    assert COMMITTED_GRAPH_CACHE_DIR != DEFAULT_GRAPH_CACHE_DIR


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
        load_values=True,
        capture_dependency_provenance=True,
    )
    changed = replace(synthetic_config, targets=("Outputs!B1",))
    changed_key = dependency_graph_cache_key(
        workbook_path=changed.workbook_path,
        targets=changed.targets,
        constraints=changed.constraints,
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
        load_values=True,
        capture_dependency_provenance=True,
    )
    changed_constraints = dict(CONSTRAINTS)
    changed_constraints["Inputs!A1"] = Annotated[float, RealBetween(0.0, 50.0)]
    changed_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=changed_constraints,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key != changed_key


def test_dependency_graph_cache_key_changes_when_blank_ranges_change(
    synthetic_config,
) -> None:
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
    )
    changed_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
        blank_ranges=("'Chart Data'!D46:X46",),
    )
    assert base_key != changed_key


def test_dependency_graph_cache_key_uses_normalized_blank_range_specs(
    synthetic_config,
) -> None:
    key_a = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
        blank_ranges=("Sheet1!B2:D4",),
    )
    key_b = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
        blank_ranges=("Sheet1!D4:B2",),
    )
    assert key_a == key_b


def test_dependency_graph_cache_key_blank_ranges_order_independent(
    synthetic_config,
) -> None:
    key_a = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
        blank_ranges=("Sheet1!A1", "Sheet1!B2:D4"),
    )
    key_b = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
        blank_ranges=("Sheet1!B2:D4", "Sheet1!A1"),
    )
    assert key_a == key_b


def test_get_or_build_dependency_graph_forwards_blank_ranges(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    blank_ranges = ("'Chart Data'!D46:X46", "Engine!Z1")
    fake_graph = MagicMock()
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    with (
        patch(
            "src.graph_cache.create_dependency_graph", return_value=fake_graph
        ) as create,
        patch("src.graph_cache.save_dependency_graph"),
    ):
        get_or_build_dependency_graph(
            workbook_path=synthetic_config.workbook_path,
            targets=synthetic_config.targets,
            constraints=synthetic_config.constraints,
            dynamic_refs=DynamicRefConfig.from_constraints(
                synthetic_config.constraints, {}
            ),
            blank_ranges=blank_ranges,
            cache_dir=graph_cache_dir,
            no_cache=True,
        )
    create.assert_called_once()
    assert create.call_args.kwargs["blank_ranges"] == blank_ranges


def test_dependency_graph_cache_key_changes_when_workbook_changes(
    synthetic_config,
    tmp_path: Path,
) -> None:
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
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
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key != changed_key


def test_bindings_fingerprint_stable_for_empty_bindings_dir(tmp_path: Path) -> None:
    empty_dir = tmp_path / "bindings"
    empty_dir.mkdir()
    digest = bindings_fingerprint(empty_dir)
    assert digest == bindings_fingerprint(empty_dir)
    assert len(digest) == 64


def test_bindings_fingerprint_stable_for_missing_bindings_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    digest = bindings_fingerprint(missing)
    assert digest == bindings_fingerprint(missing)
    assert digest == bindings_fingerprint(tmp_path / "also-missing")


def test_dependency_graph_cache_key_stable_when_bindings_change(
    synthetic_config,
    tmp_path: Path,
) -> None:
    """Bindings are not an input to graph construction (#272)."""
    base_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
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

    # Graph key ignores bindings_path; workbook/targets/constraints/blank_ranges matter.
    changed_key = dependency_graph_cache_key(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        load_values=True,
        capture_dependency_provenance=True,
    )
    assert base_key == changed_key
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


def test_dependency_graph_cache_hit_after_bindings_change(
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
    assert second.cache_hit
    assert first.cache_key == second.cache_key


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


def test_try_load_cached_dependency_graph_miss_does_not_build_or_write(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    with (
        patch("src.graph_cache.create_dependency_graph") as create,
        patch("src.graph_cache.save_dependency_graph") as save,
    ):
        result = try_load_cached_dependency_graph(
            workbook_path=synthetic_config.workbook_path,
            targets=synthetic_config.targets,
            constraints=synthetic_config.constraints,
            cache_dir=graph_cache_dir,
        )

    assert result is None
    create.assert_not_called()
    save.assert_not_called()
    assert list(graph_cache_dir.glob("*")) == []


def test_try_load_cached_dependency_graph_hit_does_not_rewrite(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    payload_path = graph_cache_dir / f"{first.cache_key}.pkl.gz"
    before = payload_path.read_bytes()
    mtime_before = payload_path.stat().st_mtime_ns

    with patch("src.graph_cache.save_dependency_graph") as save:
        loaded = try_load_cached_dependency_graph(
            workbook_path=synthetic_config.workbook_path,
            targets=synthetic_config.targets,
            constraints=synthetic_config.constraints,
            cache_dir=graph_cache_dir,
        )

    assert loaded is not None
    assert loaded.cache_hit
    assert loaded.cache_key == first.cache_key
    assert len(loaded.graph) == len(first.graph)
    save.assert_not_called()
    assert payload_path.read_bytes() == before
    assert payload_path.stat().st_mtime_ns == mtime_before


def test_try_load_cached_dependency_graph_leaves_corrupt_payload(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    payload_path = graph_cache_dir / f"{first.cache_key}.pkl.gz"
    corrupt = b"not-a-valid-gzip-pickle"
    payload_path.write_bytes(corrupt)

    loaded = try_load_cached_dependency_graph(
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        constraints=synthetic_config.constraints,
        cache_dir=graph_cache_dir,
    )

    assert loaded is None
    assert payload_path.is_file()
    assert payload_path.read_bytes() == corrupt


def test_load_dependency_graph_unlink_corrupt_false_preserves_payload(
    graph_cache_dir: Path,
) -> None:
    graph_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = "corrupt-key"
    payload_path = graph_cache_dir / f"{cache_key}.pkl.gz"
    corrupt = b"not-a-valid-gzip-pickle"
    payload_path.write_bytes(corrupt)

    assert (
        load_dependency_graph(
            cache_key,
            cache_dir=graph_cache_dir,
            unlink_corrupt=False,
        )
        is None
    )
    assert payload_path.read_bytes() == corrupt


def test_dependency_graph_cache_writes_egdg_multipart_payload(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    """Warm-cache files must use excel-grapher's low-peak EGDG format."""
    result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    payload_path = graph_cache_dir / f"{result.cache_key}.pkl.gz"

    with gzip.open(payload_path, "rb") as handle:
        magic = handle.read(4)

    assert magic == b"EGDG"


def test_load_dependency_graph_rejects_legacy_gzip_pickle(
    synthetic_config,
    graph_cache_dir: Path,
) -> None:
    """Pre-5.1.5 single-object gzip pickles are not loadable; rebuild from scratch."""
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    clear_process_dependency_graph_cache(cache_dir=graph_cache_dir)
    payload_path = graph_cache_dir / f"{first.cache_key}.pkl.gz"
    with gzip.open(payload_path, "wb", compresslevel=1) as handle:
        pickle.dump(first.graph, handle, protocol=pickle.HIGHEST_PROTOCOL)

    assert load_dependency_graph(first.cache_key, cache_dir=graph_cache_dir) is None
    assert not payload_path.is_file()

    rebuilt = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    assert not rebuilt.cache_hit
    with gzip.open(graph_cache_dir / f"{rebuilt.cache_key}.pkl.gz", "rb") as handle:
        assert handle.read(4) == b"EGDG"


def test_save_dependency_graph_uses_dump_graph(
    synthetic_config,
    graph_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    calls: list[Path] = []

    def fake_dump_graph(graph: DependencyGraph, path: str | Path, **_kwargs) -> None:
        dest = Path(path)
        calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"egdg-stub")

    monkeypatch.setattr("src.graph_cache.dump_graph", fake_dump_graph)
    save_dependency_graph(
        first.graph,
        cache_key="spy-key",
        workbook_path=synthetic_config.workbook_path,
        targets=synthetic_config.targets,
        cache_dir=graph_cache_dir,
    )

    assert calls == [graph_cache_dir / "spy-key.pkl.gz"]
    assert (graph_cache_dir / "spy-key.pkl.gz").read_bytes() == b"egdg-stub"


def test_projection_cache_roundtrip(
    synthetic_config,
    synthetic_series_bindings,
    graph_cache_dir: Path,
    projection_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    first = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
        cache_dir=projection_cache_dir,
    )
    second = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
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
    projection = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
        no_cache=True,
    ).projection
    modules = CodeGenerator(projection).generate_modules(
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
    synthetic_series_bindings,
    graph_cache_dir: Path,
    projection_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    first = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
        cache_dir=projection_cache_dir,
    )
    other_graph_key = hashlib.sha256(b"other-graph-key").hexdigest()
    second = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=other_graph_key,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
        cache_dir=projection_cache_dir,
    )
    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key


def test_rehydrate_projection_result_uses_original_graph(
    synthetic_graph,
    synthetic_series_bindings,
    synthetic_workbook_path,
) -> None:
    live = get_or_build_refactor_projection(
        synthetic_graph,
        graph_cache_key="synthetic-graph-key",
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_workbook_path,
        no_cache=True,
    ).projection
    cached = get_or_build_refactor_projection(
        synthetic_graph,
        graph_cache_key="synthetic-graph-key",
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_workbook_path,
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
    synthetic_series_bindings,
    graph_cache_dir: Path,
    projection_cache_dir: Path,
) -> None:
    graph_result = _build_graph(synthetic_config, cache_dir=graph_cache_dir)
    projection_result = get_or_build_refactor_projection(
        graph_result.graph,
        graph_cache_key=graph_result.cache_key,
        series_bindings=synthetic_series_bindings,
        bindings_workbook=synthetic_config.workbook_path,
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
        patch("src.extraction_pipeline.extract_dependency_graph") as extract,
    ):
        main(["--extract-graph", "--no-cache"])

    extract.assert_called_once_with(
        synthetic_config, no_cache=True, force_rebuild=False, timings=ANY
    )
