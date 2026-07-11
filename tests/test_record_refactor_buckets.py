"""Tests for refactor bucket recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.pipeline_config import load_pipeline_config, validate_pipeline_config
from src.pipeline_context import activate_pipeline_config
from src.record_refactor_buckets import run_record_refactor_buckets


@pytest.fixture(scope="module")
def refactor_buckets_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    config = load_pipeline_config()
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

    output_dir = tmp_path_factory.mktemp("refactor_buckets")
    activate_pipeline_config(config)
    return run_record_refactor_buckets(
        config,
        json_output=output_dir / "refactor-buckets.json",
        markdown_output=output_dir / "refactor-buckets.md",
        compression="optimal",
    )


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
    config = load_pipeline_config()
    try:
        validate_pipeline_config(config)
    except FileNotFoundError as exc:
        pytest.skip(f"Pipeline configuration is incomplete: {exc}")

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
