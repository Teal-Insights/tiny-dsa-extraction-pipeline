"""Tests for disposable ``dist/`` materialization from cached artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from src.codegen_cache import save_codegen_payload
from src.package_materialize import (
    PACKAGE_CACHE_KEYS_FILENAME,
    PackageCacheKeys,
    adopt_codegen_cache_from_dist,
    apply_export_api_rewrite,
    current_internals_inputs,
    load_pristine_internals_from_codegen,
    materialize_package,
    read_package_cache_keys,
    try_materialize_refactored_package_from_cache,
    write_package_cache_keys,
)
from src.pipeline_config import DistProjectMetadata, PipelineConfig

_SAMPLE_MODULES = {
    "__init__.py": "# init\n",
    "api.py": "from .runtime import EvalContext\n\ndef api():\n    return 1\n",
    "data.py": "DATA = {}\n",
    "runtime.py": "def run():\n    pass\n",
    "internals.py": "def cell_a1(ctx):\n    return 1.0\n",
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


def _snapshot_dist(dist_root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sorted(dist_root.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(dist_root).as_posix()] = path.read_bytes()
    return snapshot


def test_materialize_package_byte_identical_after_dist_wipe(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    codegen_key = "a" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )

    materialize_package(config, codegen_key=codegen_key)
    golden = _snapshot_dist(config.dist_root)

    shutil.rmtree(config.dist_root)
    materialize_package(config, codegen_key=codegen_key)
    rematerialized = _snapshot_dist(config.dist_root)

    assert rematerialized == golden
    assert PACKAGE_CACHE_KEYS_FILENAME in golden
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=None,
    )


def _emitted_runtime_source() -> str:
    """The real codegen ``runtime.py``, which does not export the memo API."""
    from excel_grapher.exporter.embed import emit_runtime, runtime_cache_seed_symbols

    return emit_runtime(
        set(runtime_cache_seed_symbols(include_dep_tracking=True)),
        include_offset_table=False,
    )


def test_materialize_package_keeps_runtime_memoization_api(tmp_path: Path) -> None:
    """``dist/`` must never ship internals importing a symbol runtime.py lacks.

    Mechanical refactor decorates helpers with ``@xl_memoize`` and merges the
    matching ``from .runtime import`` line into ``internals.py``. Codegen's
    ``runtime.py`` does not define that API — the refactor stage patches the file
    in place. Since ``materialize_package`` rewrites ``runtime.py`` from the
    codegen payload and runs *after* refactor, it has to re-apply the patch or it
    strips the API back out and the exported package fails to import.
    """
    from src.helper_memoization import runtime_source_has_helper_memoization

    runtime_source = _emitted_runtime_source()
    assert not runtime_source_has_helper_memoization(runtime_source)

    config = _prepare_repo(tmp_path)
    codegen_key = "a" * 64
    modules = dict(_SAMPLE_MODULES)
    modules["runtime.py"] = runtime_source
    modules["internals.py"] = (
        "from .runtime import xl_memoize\n\n\n@xl_memoize\ndef helper_x(ctx):\n"
        "    return 1.0\n"
    )
    save_codegen_payload(modules, cache_key=codegen_key, projection_cache_key="p" * 64)

    materialize_package(config, codegen_key=codegen_key)

    written = (config.package_root / "runtime.py").read_text(encoding="utf-8")
    assert runtime_source_has_helper_memoization(written)


def test_materialize_package_runtime_memoization_is_idempotent(
    tmp_path: Path,
) -> None:
    """Re-materializing must not append the polyfill twice."""
    from src.helper_memoization import runtime_source_has_helper_memoization

    config = _prepare_repo(tmp_path)
    codegen_key = "a" * 64
    modules = dict(_SAMPLE_MODULES)
    modules["runtime.py"] = _emitted_runtime_source()
    save_codegen_payload(modules, cache_key=codegen_key, projection_cache_key="p" * 64)

    materialize_package(config, codegen_key=codegen_key)
    first = (config.package_root / "runtime.py").read_text(encoding="utf-8")
    materialize_package(config, codegen_key=codegen_key)
    second = (config.package_root / "runtime.py").read_text(encoding="utf-8")

    assert first == second
    assert runtime_source_has_helper_memoization(second)
    assert second.count("def xl_memoize(") == 1


def test_materialize_package_fails_loudly_on_missing_codegen_payload(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    with pytest.raises(FileNotFoundError, match="codegen cache payload missing"):
        materialize_package(config, codegen_key="missing" * 8)


def test_pristine_oracle_ignores_refactored_dist_internals(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    codegen_key = "b" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    refactored = "def cell_a1(ctx):\n    return 99.0\n"
    (config.package_root / "internals.py").write_text(refactored, encoding="utf-8")

    pristine = load_pristine_internals_from_codegen(codegen_key)
    assert pristine == _SAMPLE_MODULES["internals.py"]
    assert pristine != refactored
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == refactored


def test_pristine_oracle_fails_when_codegen_payload_missing() -> None:
    with pytest.raises(FileNotFoundError, match="cannot resolve pristine internals"):
        load_pristine_internals_from_codegen("z" * 64)


def test_cold_cache_adoption_from_committed_dist_skips_refactor(
    tmp_path: Path,
) -> None:
    from src.extraction_pipeline import ExportStageState, run_refactor_stage

    config = _prepare_repo(tmp_path)
    codegen_key = "c" * 64
    internals_key = "d" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    refactored = "def cell_a1(ctx):\n    return 42.0\n"
    (config.package_root / "internals.py").write_text(refactored, encoding="utf-8")
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=current_internals_inputs(),
        ),
    )

    # Fresh-clone conditions: internals cache cold, committed dist present.
    # Pytest already redirects DEFAULT_INTERNALS_CACHE_DIR away from the repo.
    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

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
    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == refactored
    assert (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py").is_file()
    assert read_package_cache_keys(config.dist_root) == PackageCacheKeys(
        codegen_key=codegen_key,
        internals_key=internals_key,
        internals_inputs=current_internals_inputs(),
    )


def test_try_materialize_refactored_package_adopts_when_internals_cache_cold(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    codegen_key = "e" * 64
    internals_key = "f" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)
    refactored = "def cell_a1(ctx):\n    return 7.0\n"
    (config.package_root / "internals.py").write_text(refactored, encoding="utf-8")
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=current_internals_inputs(),
        ),
    )

    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    cache_path = DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py"
    cache_path.unlink(missing_ok=True)
    (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.meta.json").unlink(missing_ok=True)

    assert try_materialize_refactored_package_from_cache(
        config, codegen_key=codegen_key
    )

    assert (config.package_root / "internals.py").read_text(
        encoding="utf-8"
    ) == refactored
    assert cache_path.read_text(encoding="utf-8") == refactored


def test_adopt_codegen_cache_from_dist_rejects_refactored_package(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    codegen_key = "g" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(codegen_key=codegen_key, internals_key="h" * 64),
    )

    assert not adopt_codegen_cache_from_dist(
        config,
        expected_codegen_key=codegen_key,
        projection_cache_key="proj-key",
    )


def test_try_materialize_refuses_when_expected_internals_key_mismatches_sidecar(
    tmp_path: Path,
) -> None:
    """Dist-sidecar adopt must verify the content-keyed internals key (#234 review).

    Matching ``codegen_key`` alone is not enough: a stale ``internals_key`` in
    ``.pipeline-cache-keys.json`` must not be adopted when it disagrees with the
    recomputed ``internals_cache_key`` for the current clusters / digest / schemas.
    """
    config = _prepare_repo(tmp_path)
    codegen_key = "i" * 64
    sidecar_internals_key = "s" * 64
    expected_internals_key = "e" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)
    (config.package_root / "internals.py").write_text(
        "def cell_a1(ctx):\n    return 99.0\n",
        encoding="utf-8",
    )
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=sidecar_internals_key,
        ),
    )

    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    assert (
        try_materialize_refactored_package_from_cache(
            config,
            codegen_key=codegen_key,
            expected_internals_key=expected_internals_key,
        )
        is False
    )
    assert not (DEFAULT_INTERNALS_CACHE_DIR / f"{sidecar_internals_key}.py").is_file()
    assert not (DEFAULT_INTERNALS_CACHE_DIR / f"{expected_internals_key}.py").is_file()


def test_try_materialize_adopts_when_expected_internals_key_matches_sidecar(
    tmp_path: Path,
) -> None:
    """Matching content key still allows the committed-dist cold-clone path."""
    config = _prepare_repo(tmp_path)
    codegen_key = "j" * 64
    internals_key = "k" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)
    refactored = "def cell_a1(ctx):\n    return 3.0\n"
    (config.package_root / "internals.py").write_text(refactored, encoding="utf-8")
    write_package_cache_keys(
        config.dist_root,
        PackageCacheKeys(
            codegen_key=codegen_key,
            internals_key=internals_key,
            internals_inputs=current_internals_inputs(),
        ),
    )

    from src.internals_refactor import DEFAULT_INTERNALS_CACHE_DIR

    assert try_materialize_refactored_package_from_cache(
        config,
        codegen_key=codegen_key,
        expected_internals_key=internals_key,
    )
    assert (DEFAULT_INTERNALS_CACHE_DIR / f"{internals_key}.py").read_text(
        encoding="utf-8"
    ) == refactored


def test_apply_export_api_rewrite_is_idempotent() -> None:
    once = apply_export_api_rewrite(dict(_SAMPLE_MODULES))
    twice = apply_export_api_rewrite(dict(once))
    assert once["api.py"] == twice["api.py"]
