"""Tests for hand-authored ``dist-overlay/`` files and ``{package}/blank_ranges.py``."""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from src.codegen_cache import save_codegen_payload
from src.package_materialize import (
    DIST_OVERLAY_REL,
    PACKAGE_OVERLAY_MANIFEST_FILENAME,
    materialize_package,
)
from src.pipeline_config import PipelineConfig
from tests.test_package_materialize import _SAMPLE_MODULES, _prepare_repo

REPO_ROOT = Path(__file__).resolve().parents[1]
# Materialize imports graph_schema.py to lay out the series graph.
_STUB_SCHEMA = "# workbook schema\nNODES = ()\nEDGES = ()\n"


def _materialize(config: PipelineConfig) -> None:
    codegen_key = "o" * 64
    save_codegen_payload(
        _SAMPLE_MODULES,
        cache_key=codegen_key,
        projection_cache_key="proj-key",
    )
    materialize_package(config, codegen_key=codegen_key)


def _write_overlay(repo_root: Path, relative: str, text: str) -> None:
    path = repo_root / DIST_OVERLAY_REL / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _manifest(config: PipelineConfig) -> list[str]:
    path = config.dist_root / PACKAGE_OVERLAY_MANIFEST_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def test_materialize_writes_blank_ranges_module_from_config(tmp_path: Path) -> None:
    config = dataclasses.replace(
        _prepare_repo(tmp_path),
        blank_ranges=("Sheet!A1:B2", "'Other Sheet'!C3"),
    )
    _materialize(config)

    module = (config.package_root / "blank_ranges.py").read_text(encoding="utf-8")
    assignment = next(
        node
        for node in ast.parse(module).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "BLANK_RANGES"
    )
    assert assignment.value is not None
    assert ast.literal_eval(assignment.value) == config.blank_ranges


def test_materialize_writes_empty_blank_ranges_module(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _materialize(config)

    namespace: dict[str, object] = {}
    exec(  # noqa: S102
        (config.package_root / "blank_ranges.py").read_text(encoding="utf-8"),
        namespace,
    )
    assert namespace["BLANK_RANGES"] == ()


def test_dist_gitignore_ignores_local_graph_cache(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _materialize(config)

    ignored = (config.dist_root / ".gitignore").read_text(encoding="utf-8").split()
    assert ".cache/" in ignored


def test_materialize_without_overlay_writes_no_manifest(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _materialize(config)

    assert not (config.dist_root / PACKAGE_OVERLAY_MANIFEST_FILENAME).exists()


def test_overlay_replaces_seeded_series_graph_files(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "my_model/graph_schema.py", _STUB_SCHEMA)
    _write_overlay(tmp_path, "Dockerfile", "FROM python:3.13-slim\n")
    _write_overlay(tmp_path, "assets/graph/README.md", "# workbook viz\n")
    _materialize(config)

    assert (config.package_root / "graph_schema.py").read_text(
        encoding="utf-8"
    ) == _STUB_SCHEMA
    assert (config.dist_root / "Dockerfile").read_text(
        encoding="utf-8"
    ) == "FROM python:3.13-slim\n"
    assert (config.dist_root / "assets" / "graph" / "README.md").read_text(
        encoding="utf-8"
    ) == "# workbook viz\n"
    # Seeded files the overlay does not replace stay in place.
    assert (config.dist_root / "assets" / "graph" / "app.js").is_file()
    assert (config.package_root / "graph_api.py").is_file()
    assert _manifest(config) == [
        "Dockerfile",
        "assets/graph/README.md",
        "my_model/graph_schema.py",
    ]


def test_overlay_skips_pycache(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "scripts/deploy.py", "print('hi')\n")
    _write_overlay(tmp_path, "scripts/__pycache__/deploy.cpython-313.pyc", "x")
    _materialize(config)

    assert (config.dist_root / "scripts" / "deploy.py").is_file()
    assert not (config.dist_root / "scripts" / "__pycache__").exists()
    assert _manifest(config) == ["scripts/deploy.py"]


def test_files_removed_from_overlay_are_removed_from_dist(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "a.txt", "a\n")
    _write_overlay(tmp_path, "deploy/nested/b.txt", "b\n")
    _materialize(config)
    assert (config.dist_root / "deploy" / "nested" / "b.txt").is_file()

    (tmp_path / DIST_OVERLAY_REL / "deploy" / "nested" / "b.txt").unlink()
    _materialize(config)

    assert (config.dist_root / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert not (config.dist_root / "deploy").exists()
    assert _manifest(config) == ["a.txt"]


def test_emptied_overlay_removes_its_files_and_manifest(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, "LICENSE.md", "MIT\n")
    _materialize(config)

    (tmp_path / DIST_OVERLAY_REL / "LICENSE.md").unlink()
    _materialize(config)

    assert not (config.dist_root / "LICENSE.md").exists()
    assert not (config.dist_root / PACKAGE_OVERLAY_MANIFEST_FILENAME).exists()


def test_dropping_an_overlaid_seed_file_restores_the_template(tmp_path: Path) -> None:
    config = _prepare_repo(tmp_path)
    _materialize(config)
    seeded = (config.package_root / "graph_schema.py").read_bytes()

    _write_overlay(tmp_path, "my_model/graph_schema.py", _STUB_SCHEMA)
    _materialize(config)
    (tmp_path / DIST_OVERLAY_REL / "my_model" / "graph_schema.py").unlink()
    _materialize(config)

    assert (config.package_root / "graph_schema.py").read_bytes() == seeded


@pytest.mark.parametrize(
    "relative",
    [
        "my_model/api.py",
        "my_model/__init__.py",
        "my_model/blank_ranges.py",
        "pyproject.toml",
        "README.md",
        ".gitignore",
        ".pipeline-cache-keys.json",
        PACKAGE_OVERLAY_MANIFEST_FILENAME,
        ".github/workflows/deploy-docs.yml",
        "great-docs.yml",
        "uv.lock",
        "tests/README.md",
        "tests/__init__.py",
        "bindings/inputs.bindings.yaml",
        "docs-source/guidance-note.md",
        "great-docs/_site/index.html",
        "user_guide/index.qmd",
        "tests/differential/binding_adapter.py",
        "tests/fixtures/workbook.xlsx",
        "tests/results/reference/parity_report.txt",
    ],
)
def test_overlay_may_not_replace_pipeline_written_paths(
    tmp_path: Path, relative: str
) -> None:
    config = _prepare_repo(tmp_path)
    _write_overlay(tmp_path, relative, "# hand edit\n")

    with pytest.raises(ValueError, match="dist-overlay"):
        _materialize(config)


def test_committed_dist_matches_overlay() -> None:
    overlay_root = REPO_ROOT / DIST_OVERLAY_REL
    if not overlay_root.is_dir():
        pytest.skip("this repository has no dist-overlay/")
    overlay_files = sorted(
        path
        for path in overlay_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    # Checkouts may convert either side to CRLF; the committed form is LF.
    stale = [
        relative
        for relative, path in (
            (path.relative_to(overlay_root), path) for path in overlay_files
        )
        if not (REPO_ROOT / "dist" / relative).is_file()
        or (REPO_ROOT / "dist" / relative).read_bytes().replace(b"\r\n", b"\n")
        != path.read_bytes().replace(b"\r\n", b"\n")
    ]
    assert stale == [], "re-run export so dist/ picks up dist-overlay/"
