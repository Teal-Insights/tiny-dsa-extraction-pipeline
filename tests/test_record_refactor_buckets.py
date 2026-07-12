"""Tests for refactor bucket recording."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from excel_grapher.exporter import (
    FieldDoc as SeriesFieldDoc,
    SeriesFunctionDoc,
    register_series_docstring_callback,
)

from src.pipeline_config import (
    PipelineConfig,
    load_pipeline_config,
    validate_pipeline_config,
)
from src.pipeline_context import activate_pipeline_config
from src.record_refactor_buckets import (
    DEFAULT_CODEGEN_DIST_ROOT,
    main as record_refactor_buckets_main,
    run_record_refactor_buckets,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_INTERNALS = _REPO_ROOT / "dist" / "tiny_dsa" / "internals.py"


def _stub_configure_docstring_callback(config: PipelineConfig) -> str:
    def stub(ctx) -> SeriesFunctionDoc:
        return SeriesFunctionDoc(
            summary="Test stub.",
            purpose="Test stub.",
            record_matching="Test stub.",
            field_descriptions={
                field_name: SeriesFieldDoc(description="Test stub.")
                for field_name in ctx.contract.fields
            },
        )

    register_series_docstring_callback(
        config.docstring_callback_name,
        stub,
        replace=True,
    )
    return config.docstring_callback_name


def _load_validated_pipeline_config() -> PipelineConfig:
    config = load_pipeline_config()
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")
    return config


@pytest.fixture(scope="module")
def stub_docstring_callback() -> Iterator[None]:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "src.record_refactor_buckets.configure_docstring_callback",
        _stub_configure_docstring_callback,
    )
    yield
    monkeypatch.undo()


@pytest.fixture(scope="module")
def pipeline_config() -> PipelineConfig:
    return _load_validated_pipeline_config()


@pytest.fixture(scope="module")
def refactor_buckets_report(
    pipeline_config: PipelineConfig,
    tmp_path_factory: pytest.TempPathFactory,
    stub_docstring_callback: None,
) -> dict[str, Any]:
    output_dir = tmp_path_factory.mktemp("refactor_buckets")
    activate_pipeline_config(pipeline_config)
    return run_record_refactor_buckets(
        pipeline_config,
        json_output=output_dir / "refactor-buckets.json",
        markdown_output=output_dir / "refactor-buckets.md",
        compression="optimal",
    )


def test_run_record_refactor_buckets_leaves_repo_dist_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _REPO_INTERNALS.is_file():
        pytest.skip("dist/tiny_dsa/internals.py is not present")

    config = _load_validated_pipeline_config()
    before = _REPO_INTERNALS.read_bytes()

    monkeypatch.setattr(
        "src.record_refactor_buckets.configure_docstring_callback",
        _stub_configure_docstring_callback,
    )
    activate_pipeline_config(config)
    run_record_refactor_buckets(
        config,
        json_output=tmp_path / "refactor-buckets.json",
        markdown_output=tmp_path / "refactor-buckets.md",
        compression="optimal",
    )

    assert _REPO_INTERNALS.read_bytes() == before
    codegen_internals = (
        config.repo_root
        / DEFAULT_CODEGEN_DIST_ROOT
        / config.dist_metadata.package_name
        / "internals.py"
    )
    assert codegen_internals.is_file()


def test_record_refactor_buckets_main_leaves_repo_dist_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _REPO_INTERNALS.is_file():
        pytest.skip("dist/tiny_dsa/internals.py is not present")

    config = _load_validated_pipeline_config()
    before = _REPO_INTERNALS.read_bytes()

    monkeypatch.setattr(
        "src.record_refactor_buckets.configure_docstring_callback",
        _stub_configure_docstring_callback,
    )
    record_refactor_buckets_main(
        [
            "--json-output",
            str(tmp_path / "refactor-buckets.json"),
            "--markdown-output",
            str(tmp_path / "refactor-buckets.md"),
        ]
    )

    assert _REPO_INTERNALS.read_bytes() == before
    codegen_internals = (
        config.repo_root
        / DEFAULT_CODEGEN_DIST_ROOT
        / config.dist_metadata.package_name
        / "internals.py"
    )
    assert codegen_internals.is_file()


def test_refactor_buckets_cover_all_eligible_targets(
    refactor_buckets_report: dict[str, Any],
) -> None:
    assert refactor_buckets_report["cluster_count"] == 9
    assert refactor_buckets_report["refactor_target_count"] == 9
    assert refactor_buckets_report["skipped_target_count"] == 0


def test_refactor_buckets_include_expected_singleton_and_cluster_members(
    refactor_buckets_report: dict[str, Any],
) -> None:
    buckets = refactor_buckets_report["buckets"]
    by_cluster_id = {bucket["cluster_id"]: bucket for bucket in buckets}

    assert by_cluster_id[0]["kind"] == "singleton"
    assert by_cluster_id[0]["members"] == ["Engine!B9"]

    assert by_cluster_id[1]["kind"] == "cluster"
    assert by_cluster_id[1]["members"] == [
        "Engine!C10",
        "Engine!D10",
        "Engine!E10",
        "Engine!F10",
        "Engine!G10",
    ]

    assert by_cluster_id[8]["members"] == [
        "Outputs!B14",
        "Outputs!C14",
        "Outputs!D14",
        "Outputs!E14",
        "Outputs!F14",
    ]


def test_uncompressed_refactor_buckets_include_shocked_parameter_rows(
    tmp_path: Path,
) -> None:
    config = _load_validated_pipeline_config()

    activate_pipeline_config(config)
    report = run_record_refactor_buckets(
        config,
        json_output=tmp_path / "refactor-buckets-uncompressed.json",
        markdown_output=tmp_path / "refactor-buckets-uncompressed.md",
        compression="none",
    )

    assert report["compression"] == "none"
    assert report["cluster_count"] == 11
    members = {address for bucket in report["buckets"] for address in bucket["members"]}
    assert "Engine!C14" in members
    assert "Engine!C15" in members
