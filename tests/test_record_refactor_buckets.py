"""Tests for refactor bucket recording."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
    main,
    record_refactor_buckets,
    run_record_refactor_buckets,
)
from tests.fixtures.inter_cluster_cycle import inter_cluster_cycle_graph
from tests.fixtures.synthetic_pipeline import synthetic_pipeline_config


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
        codegen_dist_root=output_dir / "codegen",
    )


def test_run_record_refactor_buckets_writes_codegen_outside_dist(
    synthetic_workbook_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist_root = tmp_path / "dist"
    codegen_root = tmp_path / "codegen"
    config = replace(
        synthetic_pipeline_config(workbook_path=synthetic_workbook_path),
        dist_root=dist_root,
    )
    package_internals = config.package_root / "internals.py"
    package_internals.parent.mkdir(parents=True, exist_ok=True)
    package_internals.write_text("# sentinel\n", encoding="utf-8")
    before = package_internals.read_bytes()

    monkeypatch.setattr(
        "src.record_refactor_buckets.configure_docstring_callback",
        _stub_configure_docstring_callback,
    )
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ("xl_cell", "xl_compare"),
    )
    activate_pipeline_config(config)
    report = run_record_refactor_buckets(
        config,
        json_output=tmp_path / "refactor-buckets.json",
        markdown_output=tmp_path / "refactor-buckets.md",
        compression="optimal",
        codegen_dist_root=codegen_root,
    )

    assert package_internals.read_bytes() == before
    codegen_internals = (
        codegen_root / config.dist_metadata.package_name / "internals.py"
    )
    assert codegen_internals.is_file()
    assert report["internals_path"] == config.repo_relative_posix_path(
        codegen_internals
    )


def test_run_record_refactor_buckets_leaves_repo_dist_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_validated_pipeline_config()
    repo_internals = config.package_root / "internals.py"
    if not repo_internals.is_file():
        pytest.skip(f"{repo_internals.as_posix()} is not present")

    before = repo_internals.read_bytes()

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

    assert repo_internals.read_bytes() == before
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
    config = _load_validated_pipeline_config()
    repo_internals = config.package_root / "internals.py"
    if not repo_internals.is_file():
        pytest.skip(f"{repo_internals.as_posix()} is not present")

    before = repo_internals.read_bytes()

    monkeypatch.setattr(
        "src.record_refactor_buckets.configure_docstring_callback",
        _stub_configure_docstring_callback,
    )
    main(
        [
            "--json-output",
            str(tmp_path / "refactor-buckets.json"),
            "--markdown-output",
            str(tmp_path / "refactor-buckets.md"),
        ]
    )

    assert repo_internals.read_bytes() == before
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
    assert refactor_buckets_report["formula_cluster_count"] == 9
    assert refactor_buckets_report["refactor_unit_count"] == 9
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


def test_refactor_buckets_record_contract_for_cluster_targets(
    refactor_buckets_report: dict[str, Any],
) -> None:
    buckets = refactor_buckets_report["buckets"]
    cluster_buckets = [
        bucket
        for bucket in buckets
        if bucket["kind"] == "cluster" and bucket["eligible"]
    ]
    assert cluster_buckets
    assert all(bucket["contract"] == "member_sweep" for bucket in cluster_buckets)
    singleton_buckets = [bucket for bucket in buckets if bucket["kind"] == "singleton"]
    assert all(bucket["contract"] is None for bucket in singleton_buckets)


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
    assert report["formula_cluster_count"] == 11
    assert report["refactor_unit_count"] == 11
    assert report["cluster_count"] == 11
    members = {address for bucket in report["buckets"] for address in bucket["members"]}
    assert "Engine!C14" in members
    assert "Engine!C15" in members


def test_main_passes_cli_variation_mode_to_bucket_recording(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    with patch(
        "src.record_refactor_buckets.load_pipeline_config",
        return_value=synthetic_pipeline_config_fixture,
    ):
        with patch("src.record_refactor_buckets.validate_pipeline_config"):
            with patch("src.record_refactor_buckets.activate_pipeline_config"):
                with patch(
                    "src.record_refactor_buckets.run_record_refactor_buckets"
                ) as run_buckets:
                    run_buckets.return_value = {
                        "formula_cluster_count": 0,
                        "refactor_unit_count": 0,
                        "cluster_count": 0,
                        "refactor_target_count": 0,
                        "skipped_target_count": 0,
                        "buckets": [],
                    }
                    main(
                        [
                            "--variation-mode",
                            "dominant_key_only",
                            "--json-output",
                            str(tmp_path / "buckets.json"),
                            "--markdown-output",
                            str(tmp_path / "buckets.md"),
                        ]
                    )

    run_buckets.assert_called_once()
    assert run_buckets.call_args.args[0].variation_mode == "dominant_key_only"


def test_record_refactor_buckets_requires_bound_address_keys(
    synthetic_pipeline_config_fixture,
    synthetic_projection,
) -> None:
    with pytest.raises(ValueError, match="bound_address_keys is required"):
        record_refactor_buckets(
            synthetic_pipeline_config_fixture,
            graph=synthetic_projection,
            internals_path=None,
            internal_binding_index=None,
            layout=None,
            compression="none",
            bound_address_keys=None,
        )


def test_record_refactor_buckets_schedules_inter_cluster_cycle_mcve(
    synthetic_pipeline_config_fixture,
) -> None:
    graph, bindings = inter_cluster_cycle_graph()
    records = record_refactor_buckets(
        synthetic_pipeline_config_fixture,
        graph=graph,
        internals_path=None,
        internal_binding_index=None,
        layout=None,
        compression="none",
        bound_address_keys=bindings,
    )

    assert len(records) == 4
    assert len({record.refactor_group_id for record in records}) == 4
    assert len({record.cluster_id for record in records}) == 2
    assert [record.members for record in records] == [
        ("Engine!B2",),
        ("Engine!C2",),
        ("Engine!B3",),
        ("Engine!C3",),
    ]
