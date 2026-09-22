"""Tests for disposable ``dist/`` materialization from cached artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.codegen_cache import save_codegen_payload
from src.package_materialize import (
    PACKAGE_CACHE_KEYS_FILENAME,
    PackageCacheKeys,
    adopt_codegen_cache_from_dist,
    materialize_package,
    read_package_cache_keys,
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
        "binding_adapter.py",
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
    )


def test_materialize_package_replaces_dist_bindings_and_deletes_extras(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    nested = config.bindings_path / "shards"
    nested.mkdir()
    (nested / "gap.bindings.yaml").write_text("series: []\n", encoding="utf-8")
    (config.bindings_path / "outputs.bindings.yaml").write_text(
        "series: [kept]\n", encoding="utf-8"
    )
    stale = config.dist_root / "bindings"
    stale.mkdir(parents=True)
    (stale / "obsolete.bindings.yaml").write_text("gone\n", encoding="utf-8")
    stale_nested = stale / "old-shards"
    stale_nested.mkdir()
    (stale_nested / "retired.yaml").write_text("gone\n", encoding="utf-8")

    codegen_key = "b" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    dest = config.dist_root / "bindings"
    assert (dest / "inputs.bindings.yaml").read_text(encoding="utf-8") == "series: []\n"
    assert (dest / "outputs.bindings.yaml").read_text(encoding="utf-8") == (
        "series: [kept]\n"
    )
    assert (dest / "shards" / "gap.bindings.yaml").read_text(encoding="utf-8") == (
        "series: []\n"
    )
    assert not (dest / "obsolete.bindings.yaml").exists()
    assert not stale_nested.exists()

    (config.bindings_path / "outputs.bindings.yaml").unlink()
    (config.bindings_path / "inputs.bindings.yaml").write_text(
        "series: [updated]\n", encoding="utf-8"
    )
    (dest / "leftover.bindings.yaml").write_text("stale\n", encoding="utf-8")
    materialize_package(config, codegen_key=codegen_key)

    assert (dest / "inputs.bindings.yaml").read_text(encoding="utf-8") == (
        "series: [updated]\n"
    )
    assert (dest / "shards" / "gap.bindings.yaml").is_file()
    assert not (dest / "outputs.bindings.yaml").exists()
    assert not (dest / "leftover.bindings.yaml").exists()


def test_materialize_package_fails_loudly_on_missing_codegen_payload(
    tmp_path: Path,
) -> None:
    config = _prepare_repo(tmp_path)
    with pytest.raises(FileNotFoundError, match="codegen cache payload missing"):
        materialize_package(config, codegen_key="missing" * 8)


def test_adopt_codegen_cache_from_dist_when_keys_match(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    codegen_key = "g" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    assert adopt_codegen_cache_from_dist(
        config,
        expected_codegen_key=codegen_key,
        projection_cache_key="proj-key",
    )


def test_adopt_codegen_cache_from_dist_rejects_mismatched_key(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    codegen_key = "g" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)

    assert not adopt_codegen_cache_from_dist(
        config,
        expected_codegen_key="z" * 64,
        projection_cache_key="proj-key",
    )
