"""Tests for content-keyed refactored ``internals.py`` cache (issue #239)."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, patch

from src.codegen_cache import save_codegen_payload
from src.internals_cache import (
    INTERNALS_CACHE_SCHEMA_VERSION,
    InternalsCacheResult,
    clear_internals_cache,
    consumed_refactors_digest,
    get_or_load_refactored_internals,
    internals_cache_key,
    load_refactored_internals_payload,
    save_refactored_internals_payload,
)
from src.mechanical_body import MECHANICAL_BODY_SCHEMA_VERSION
from src.package_materialize import (
    PackageCacheKeys,
    current_internals_inputs,
    materialize_package,
    read_package_cache_keys,
    write_package_cache_keys,
)
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.refactor_parity_gate import PARITY_GATE_SCHEMA_VERSION

_SAMPLE_MODULES = {
    "__init__.py": "# init\n",
    "api.py": "from .runtime import EvalContext\n\ndef api():\n    return 1\n",
    "data.py": "DATA = {}\n",
    "runtime.py": "def run():\n    pass\n",
    "internals.py": "def cell_a1(ctx):\n    return 1.0\n",
}
_REFACTORED = "def cell_a1(ctx):\n    return 42.0\n"


class _InternalsKeyKwargs(TypedDict):
    codegen_cache_key: str
    clusters_cache_key: str
    consumed_refactors_digest: str
    mechanical_body_schema_version: str
    parity_gate_schema_version: str
    mechanical_refactor_bodies: str
    refactor_model: str
    excel_grapher_version: str


def _key_kwargs(
    *,
    codegen_cache_key: str = "a" * 64,
    clusters_cache_key: str = "b" * 64,
    consumed_refactors_digest: str = "c" * 64,
    mechanical_body_schema_version: str = MECHANICAL_BODY_SCHEMA_VERSION,
    parity_gate_schema_version: str = PARITY_GATE_SCHEMA_VERSION,
    mechanical_refactor_bodies: str = "1",
    refactor_model: str | None = None,
    excel_grapher_version: str | None = None,
) -> _InternalsKeyKwargs:
    from src.internals_refactor import refactor_model as current_refactor_model

    return {
        "codegen_cache_key": codegen_cache_key,
        "clusters_cache_key": clusters_cache_key,
        "consumed_refactors_digest": consumed_refactors_digest,
        "mechanical_body_schema_version": mechanical_body_schema_version,
        "parity_gate_schema_version": parity_gate_schema_version,
        "mechanical_refactor_bodies": mechanical_refactor_bodies,
        "refactor_model": (
            current_refactor_model() if refactor_model is None else refactor_model
        ),
        "excel_grapher_version": (
            excel_grapher_version
            if excel_grapher_version is not None
            else version("excel-grapher")
        ),
    }


def _sample_config(repo_root: Path) -> PipelineConfig:
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=repo_root / "data" / "workbook.xlsx",
        guide_path=repo_root / "data" / "guide.md",
        bindings_path=repo_root / "bindings",
        dist_root=repo_root / "dist",
        targets=("Sheet!A1",),
        constraints={},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
        docstring_callback_name="series_docs",
        binding_authoring_prompt_path=repo_root
        / "templates"
        / "binding-authoring-prompt.txt",
        user_guide_agent_prompt_path=repo_root / "templates" / "user-guide-agent.txt",
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=repo_root / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
    )


def _prepare_repo(tmp_path: Path) -> PipelineConfig:
    config = _sample_config(tmp_path)
    config.workbook_path.parent.mkdir(parents=True, exist_ok=True)
    config.workbook_path.write_bytes(b"fake-xlsx")
    config.guide_path.write_text("guide\n", encoding="utf-8")
    config.bindings_path.mkdir(parents=True, exist_ok=True)
    (config.bindings_path / "inputs.bindings.yaml").write_text(
        "series: []\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "differential").mkdir(parents=True)
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "differential" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    for name in (
        "differential_types.py",
        "differential_excel.py",
        "comparison_utils.py",
        "differential_scenario_inputs.py",
        "differential_test_exported_library.py",
    ):
        (tmp_path / "tests" / "differential" / name).write_text(
            f"# {name}\n", encoding="utf-8"
        )
    return config


def test_internals_cache_key_miss_when_any_component_changes() -> None:
    base = internals_cache_key(**_key_kwargs())
    assert base != internals_cache_key(**_key_kwargs(codegen_cache_key="z" * 64))
    assert base != internals_cache_key(**_key_kwargs(clusters_cache_key="z" * 64))
    assert base != internals_cache_key(
        **_key_kwargs(consumed_refactors_digest="z" * 64)
    )
    assert base != internals_cache_key(
        **_key_kwargs(mechanical_body_schema_version="9.9.9")
    )
    assert base != internals_cache_key(
        **_key_kwargs(parity_gate_schema_version="9.9.9")
    )
    assert base != internals_cache_key(**_key_kwargs(mechanical_refactor_bodies="0"))
    assert base != internals_cache_key(**_key_kwargs(refactor_model="gpt-other"))
    assert base != internals_cache_key(
        **_key_kwargs(excel_grapher_version="0.0.0-test")
    )


def test_consumed_refactors_digest_changes_when_cache_entry_changes() -> None:
    first = consumed_refactors_digest({"aaa": '{"helper": "one"}'})
    second = consumed_refactors_digest({"aaa": '{"helper": "two"}'})
    assert first != second
    assert consumed_refactors_digest({}) != first


def test_save_and_load_refactored_internals_round_trip(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "internals"
    cache_key = internals_cache_key(**_key_kwargs())
    save_refactored_internals_payload(
        _REFACTORED,
        cache_key=cache_key,
        codegen_cache_key="a" * 64,
        clusters_cache_key="b" * 64,
        consumed_refactors_digest="c" * 64,
        cache_dir=cache_dir,
    )
    loaded = load_refactored_internals_payload(cache_key, cache_dir=cache_dir)
    assert loaded == _REFACTORED
    assert (cache_dir / f"{cache_key}.meta.json").is_file()


def test_get_or_load_hit_skips_build(tmp_path: Path) -> None:
    cache_dir = tmp_path / "internals"
    kwargs = _key_kwargs()
    cache_key = internals_cache_key(**kwargs)
    save_refactored_internals_payload(
        _REFACTORED,
        cache_key=cache_key,
        codegen_cache_key=kwargs["codegen_cache_key"],
        clusters_cache_key=kwargs["clusters_cache_key"],
        consumed_refactors_digest=kwargs["consumed_refactors_digest"],
        cache_dir=cache_dir,
    )
    build = MagicMock(return_value=_REFACTORED)
    result = get_or_load_refactored_internals(
        **kwargs,
        build_module=build,
        cache_dir=cache_dir,
    )
    assert isinstance(result, InternalsCacheResult)
    assert result.cache_hit
    assert result.source == _REFACTORED
    assert result.cache_key == cache_key
    build.assert_not_called()


def test_get_or_load_miss_builds_and_saves(tmp_path: Path) -> None:
    cache_dir = tmp_path / "internals"
    kwargs = _key_kwargs()
    build = MagicMock(return_value=_REFACTORED)
    result = get_or_load_refactored_internals(
        **kwargs,
        build_module=build,
        cache_dir=cache_dir,
    )
    assert not result.cache_hit
    assert result.source == _REFACTORED
    build.assert_called_once()
    assert load_refactored_internals_payload(result.cache_key, cache_dir=cache_dir) == (
        _REFACTORED
    )


def test_get_or_load_no_cache_bypasses_disk(tmp_path: Path) -> None:
    cache_dir = tmp_path / "internals"
    kwargs = _key_kwargs()
    build = MagicMock(return_value=_REFACTORED)
    result = get_or_load_refactored_internals(
        **kwargs,
        build_module=build,
        cache_dir=cache_dir,
        no_cache=True,
    )
    assert not result.cache_hit
    build.assert_called_once()
    assert load_refactored_internals_payload(result.cache_key, cache_dir=cache_dir) is (
        None
    )


def test_get_or_load_force_rebuild_ignores_hit(tmp_path: Path) -> None:
    cache_dir = tmp_path / "internals"
    kwargs = _key_kwargs()
    cache_key = internals_cache_key(**kwargs)
    save_refactored_internals_payload(
        "def cell_a1(ctx):\n    return 1.0\n",
        cache_key=cache_key,
        codegen_cache_key=kwargs["codegen_cache_key"],
        clusters_cache_key=kwargs["clusters_cache_key"],
        consumed_refactors_digest=kwargs["consumed_refactors_digest"],
        cache_dir=cache_dir,
    )
    build = MagicMock(return_value=_REFACTORED)
    result = get_or_load_refactored_internals(
        **kwargs,
        build_module=build,
        cache_dir=cache_dir,
        force_rebuild=True,
    )
    assert not result.cache_hit
    assert result.source == _REFACTORED
    build.assert_called_once()


def test_get_or_load_miss_when_codegen_key_changes(tmp_path: Path) -> None:
    cache_dir = tmp_path / "internals"
    build = MagicMock(side_effect=["first\n", "second\n"])
    first = get_or_load_refactored_internals(
        **_key_kwargs(codegen_cache_key="a" * 64),
        build_module=build,
        cache_dir=cache_dir,
    )
    second = get_or_load_refactored_internals(
        **_key_kwargs(codegen_cache_key="z" * 64),
        build_module=build,
        cache_dir=cache_dir,
    )
    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key
    assert build.call_count == 2


def test_run_refactor_stage_warm_hit_skips_pass1_parity_and_pass2(
    tmp_path: Path,
) -> None:
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import (
        ExportStageArtifacts,
        ExportStageState,
        run_refactor_stage,
    )
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    config = _prepare_repo(tmp_path)
    codegen_key = "a" * 64
    clusters_key = "b" * 64
    digest = consumed_refactors_digest({})
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    cache_key = internals_cache_key(
        **_key_kwargs(
            codegen_cache_key=codegen_key,
            clusters_cache_key=clusters_key,
            consumed_refactors_digest=digest,
        )
    )
    save_refactored_internals_payload(
        _REFACTORED,
        cache_key=cache_key,
        codegen_cache_key=codegen_key,
        clusters_cache_key=clusters_key,
        consumed_refactors_digest=digest,
        cache_dir=DEFAULT_INTERNALS_CACHE_DIR,
    )

    state = ExportStageState(
        config=config,
        graph_cache_key="graph-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key=codegen_key,
        package_root=config.package_root,
    )
    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key=clusters_key,
        cache_hit=True,
        elapsed_seconds=0.01,
    )
    artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )
    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=artifacts,
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch("src.internals_refactor.refactor_internals_all_clusters") as refactor,
        patch(
            "src.internals_cache.consumed_refactors_digest",
            return_value=digest,
        ),
    ):
        run_refactor_stage(state)

    refactor.assert_not_called()
    assert (config.package_root / "internals.py").read_text(encoding="utf-8") == (
        _REFACTORED
    )
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=cache_key,
        internals_inputs=current_internals_inputs(),
    )


def test_run_refactor_stage_saves_cacheable_result_and_materializes(
    tmp_path: Path,
) -> None:
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import (
        ExportStageArtifacts,
        ExportStageState,
        run_refactor_stage,
    )
    from src.internals_refactor import (
        DEFAULT_INTERNALS_CACHE_DIR,
        InternalsRefactorRunResult,
    )

    config = _prepare_repo(tmp_path)
    codegen_key = "d" * 64
    clusters_key = "e" * 64
    digest = "f" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    state = ExportStageState(
        config=config,
        graph_cache_key="graph-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key=codegen_key,
        package_root=config.package_root,
    )
    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key=clusters_key,
        cache_hit=True,
        elapsed_seconds=0.01,
    )
    expected_key = internals_cache_key(
        **_key_kwargs(
            codegen_cache_key=codegen_key,
            clusters_cache_key=clusters_key,
            consumed_refactors_digest=digest,
        )
    )

    def _fake_refactor(*_args: object, **_kwargs: object) -> InternalsRefactorRunResult:
        path = config.package_root / "internals.py"
        path.write_text(_REFACTORED, encoding="utf-8")
        return InternalsRefactorRunResult(
            apply_results=(),
            final_source=_REFACTORED,
            cacheable=True,
        )

    artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )
    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=artifacts,
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch(
            "src.internals_refactor.refactor_internals_all_clusters",
            side_effect=_fake_refactor,
        ),
        patch(
            "src.internals_cache.consumed_refactors_digest",
            return_value=digest,
        ),
    ):
        run_refactor_stage(state)

    assert (DEFAULT_INTERNALS_CACHE_DIR / f"{expected_key}.py").read_text(
        encoding="utf-8"
    ) == _REFACTORED
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=expected_key,
        internals_inputs=current_internals_inputs(),
    )


def test_run_refactor_stage_does_not_cache_when_not_cacheable(
    tmp_path: Path,
) -> None:
    from src.cluster_cache import ClusterCacheResult
    from src.extraction_pipeline import (
        ExportStageArtifacts,
        ExportStageState,
        run_refactor_stage,
    )
    from src.internals_refactor import (
        DEFAULT_INTERNALS_CACHE_DIR,
        InternalsRefactorRunResult,
    )

    config = _prepare_repo(tmp_path)
    codegen_key = "1" * 64
    clusters_key = "2" * 64
    digest = "3" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)
    state = ExportStageState(
        config=config,
        graph_cache_key="graph-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key=codegen_key,
        package_root=config.package_root,
    )
    cluster_result = ClusterCacheResult(
        clusters=(),
        schedule=(),
        cache_key=clusters_key,
        cache_hit=True,
        elapsed_seconds=0.01,
    )
    expected_key = internals_cache_key(
        **_key_kwargs(
            codegen_cache_key=codegen_key,
            clusters_cache_key=clusters_key,
            consumed_refactors_digest=digest,
        )
    )

    def _fake_refactor(*_args: object, **_kwargs: object) -> InternalsRefactorRunResult:
        path = config.package_root / "internals.py"
        path.write_text(_REFACTORED, encoding="utf-8")
        return InternalsRefactorRunResult(
            apply_results=(),
            final_source=_REFACTORED,
            cacheable=False,
        )

    artifacts = ExportStageArtifacts(
        graph=MagicMock(),
        refactor_projection=MagicMock(),
        internal_binding_index={},
        bound_address_keys={},
        address_to_series_id={},
    )
    with (
        patch(
            "excel_grapher.series_bindings.load_series_bindings",
            return_value=MagicMock(),
        ),
        patch(
            "src.refactor_bindings.key_concept_vocabulary_from_bindings",
            return_value=(),
        ),
        patch(
            "src.extraction_pipeline.load_export_stage_artifacts",
            return_value=artifacts,
        ),
        patch(
            "src.cluster_cache.get_or_build_clusters_and_schedule",
            return_value=cluster_result,
        ),
        patch(
            "src.internals_refactor.refactor_internals_all_clusters",
            side_effect=_fake_refactor,
        ),
        patch(
            "src.internals_cache.consumed_refactors_digest",
            return_value=digest,
        ),
    ):
        run_refactor_stage(state)

    assert not (DEFAULT_INTERNALS_CACHE_DIR / f"{expected_key}.py").is_file()
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=None,
    )


def test_schema_version_constants_exist() -> None:
    assert MECHANICAL_BODY_SCHEMA_VERSION
    assert PARITY_GATE_SCHEMA_VERSION
    assert INTERNALS_CACHE_SCHEMA_VERSION


def test_cold_adopt_still_skips_refactor_when_sidecar_present(
    tmp_path: Path,
) -> None:
    """Phase 3a fresh-clone path: trust committed dist keys without clustering."""
    from src.extraction_pipeline import ExportStageState, run_refactor_stage
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    config = _prepare_repo(tmp_path)
    codegen_key = "c" * 64
    internals_key = "d" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)
    (config.package_root / "internals.py").write_text(_REFACTORED, encoding="utf-8")
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=current_internals_inputs(),
        ),
    )
    state = ExportStageState(
        config=config,
        graph_cache_key="graph-key",
        projection_cache_key="proj-key",
        series_derived_cache_key="derived-key",
        codegen_cache_key=codegen_key,
        package_root=config.package_root,
    )
    with (
        patch("src.cluster_cache.get_or_build_clusters_and_schedule") as cluster_build,
        patch("src.internals_refactor.refactor_internals_all_clusters") as refactor,
    ):
        run_refactor_stage(state)

    cluster_build.assert_not_called()
    refactor.assert_not_called()
    assert (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py").is_file()


def test_clear_internals_cache_removes_entries_and_checkpoints(tmp_path: Path) -> None:
    """Clearing must take the Pass 1 checkpoint subdirectories with it.

    The checkpoint is crash-recovery state for one specific pristine module, so
    it is stale the moment the upstream caches are dropped. Only unlinking
    top-level files would leave those namespace directories behind forever.
    """
    cache_dir = tmp_path / "internals"
    cache_dir.mkdir()
    entry = cache_dir / f"{'a' * 64}.py"
    entry.write_text("def cell_a1(ctx):\n    return 1.0\n", encoding="utf-8")
    meta = cache_dir / f"{'a' * 64}.meta.json"
    meta.write_text("{}\n", encoding="utf-8")
    checkpoint = cache_dir / "ns0123456789abcd" / "internals.mechanical.py"
    checkpoint.parent.mkdir()
    checkpoint.write_text("# checkpoint\n", encoding="utf-8")

    removed = clear_internals_cache(cache_dir=cache_dir)

    assert not entry.is_file()
    assert not meta.is_file()
    assert not checkpoint.parent.exists()
    assert set(removed) == {entry.name, meta.name, "ns0123456789abcd/"}
    assert cache_dir.is_dir()


def test_clear_internals_cache_is_a_noop_when_cache_is_absent(tmp_path: Path) -> None:
    assert clear_internals_cache(cache_dir=tmp_path / "missing") == []
