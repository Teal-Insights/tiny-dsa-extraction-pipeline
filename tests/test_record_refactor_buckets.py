"""Tests for refactor bucket recording."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from excel_grapher.exporter import (
    FieldDoc as SeriesFieldDoc,
    ProjectionResult,
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
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: ("XlError", "xl_cell", "xl_eval"),
    )
    yield
    monkeypatch.undo()


@pytest.fixture(scope="module")
def pipeline_config(synthetic_workbook_path: Path) -> PipelineConfig:
    return replace(
        synthetic_pipeline_config(workbook_path=synthetic_workbook_path),
        internal_binding_validation_mode="off",
        clustering_mode="series_ast",
    )


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
        lambda: ("XlError", "xl_cell", "xl_eval"),
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
    assert refactor_buckets_report["formula_cluster_count"] > 0
    assert refactor_buckets_report["refactor_unit_count"] > 0
    assert (
        refactor_buckets_report["refactor_target_count"]
        + refactor_buckets_report["skipped_target_count"]
        == refactor_buckets_report["cluster_count"]
    )
    assert (
        refactor_buckets_report["cluster_count"]
        == refactor_buckets_report["refactor_unit_count"]
    )


def test_refactor_buckets_include_expected_singleton_and_cluster_members(
    refactor_buckets_report: dict[str, Any],
) -> None:
    buckets = refactor_buckets_report["buckets"]
    members = {address for bucket in buckets for address in bucket["members"]}
    assert "Engine!B2" in members
    assert "Engine!C2" in members
    assert "Outputs!B1" in members
    assert "Outputs!C1" in members
    # series_ast keeps parallel engine cells in separate series-owned units
    engine_buckets = [
        bucket
        for bucket in buckets
        if set(bucket["members"]) & {"Engine!B2", "Engine!C2"}
    ]
    assert len(engine_buckets) == 2
    assert all(bucket["kind"] == "singleton" for bucket in engine_buckets)
    assert {frozenset(bucket["series_ids"]) for bucket in engine_buckets} == {
        frozenset({"engine_b2"}),
        frozenset({"engine_c2"}),
    }


def test_refactor_buckets_include_series_ids_for_internal_members(
    refactor_buckets_report: dict[str, Any],
) -> None:
    engine_buckets = [
        bucket
        for bucket in refactor_buckets_report["buckets"]
        if any(address.startswith("Engine!") for address in bucket["members"])
    ]
    assert engine_buckets
    assert all(bucket["series_ids"] for bucket in engine_buckets)
    assert any("engine_b2" in bucket["series_ids"] for bucket in engine_buckets)


def test_refactor_buckets_include_public_output_series_ids(
    refactor_buckets_report: dict[str, Any],
) -> None:
    output_buckets = [
        bucket
        for bucket in refactor_buckets_report["buckets"]
        if any(address.startswith("Outputs!") for address in bucket["members"])
    ]
    assert output_buckets
    assert any(
        series_id in {"result_a", "result_b"}
        for bucket in output_buckets
        for series_id in bucket["series_ids"]
    )


def test_refactor_buckets_document_series_partition_note(
    refactor_buckets_report: dict[str, Any],
) -> None:
    note = refactor_buckets_report["series_partition_note"]
    assert "not automatically singleton" in note
    assert "_ADDRESS_DISPATCH" in note
    from src.record_refactor_buckets import render_refactor_buckets_markdown

    markdown = render_refactor_buckets_markdown(refactor_buckets_report)
    assert "## Series partition notes" in markdown
    assert "not automatically singleton" in markdown


def test_member_engine_column_falls_back_without_projection_layout() -> None:
    from src.record_refactor_buckets import _member_engine_column

    assert _member_engine_column("'Hot Adapted'!AA11", None) == "AA"
    assert _member_engine_column("Outputs!B14", None) == "B"


def test_cluster_contract_resolves_without_projection_layout(
    synthetic_graph,
    synthetic_bound_address_keys,
    synthetic_pipeline_config_fixture: PipelineConfig,
) -> None:
    from src.formula_clustering import FormulaCluster
    from src.internals_refactor import address_to_function_name
    from src.record_refactor_buckets import _cluster_contract_and_skip_reason
    from src.refactor_bindings import load_key_concept_vocabulary

    members = ("Outputs!B1", "Outputs!C1")
    internals_source = "\n".join(
        f"def {address_to_function_name(address)}(ctx):\n    return 0\n"
        for address in members
    )
    cluster = FormulaCluster(
        cluster_id=0,
        members=members,
        canonical_template="=Engine!B2",
        row=1,
    )
    contract, skip_reason = _cluster_contract_and_skip_reason(
        synthetic_graph,
        cluster,
        internals_source,
        layout=None,
        bound_address_keys=synthetic_bound_address_keys,
        key_vocabulary=load_key_concept_vocabulary(
            synthetic_pipeline_config_fixture.bindings_path
        ),
        workbook_path=synthetic_pipeline_config_fixture.workbook_path,
    )
    assert skip_reason is None
    assert contract == "member_sweep"


def test_refactor_buckets_record_contract_for_cluster_targets(
    refactor_buckets_report: dict[str, Any],
) -> None:
    buckets = refactor_buckets_report["buckets"]
    cluster_buckets = [
        bucket
        for bucket in buckets
        if bucket["kind"] == "cluster" and bucket["eligible"]
    ]
    if cluster_buckets:
        assert all(bucket["contract"] == "member_sweep" for bucket in cluster_buckets)
    singleton_buckets = [bucket for bucket in buckets if bucket["kind"] == "singleton"]
    assert all(bucket["contract"] is None for bucket in singleton_buckets)


def test_uncompressed_refactor_buckets_include_formula_cells(
    pipeline_config: PipelineConfig,
    tmp_path: Path,
    stub_docstring_callback: None,
) -> None:
    activate_pipeline_config(pipeline_config)
    report = run_record_refactor_buckets(
        pipeline_config,
        json_output=tmp_path / "refactor-buckets-uncompressed.json",
        markdown_output=tmp_path / "refactor-buckets-uncompressed.md",
        compression="none",
    )

    assert report["compression"] == "none"
    assert report["formula_cluster_count"] > 0
    assert report["refactor_unit_count"] > 0
    assert report["cluster_count"] == report["refactor_unit_count"]
    members = {address for bucket in report["buckets"] for address in bucket["members"]}
    assert "Engine!B2" in members or "Engine!C2" in members


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


def test_main_passes_cli_clustering_mode_to_bucket_recording(
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
                            "--clustering-mode",
                            "ast",
                            "--json-output",
                            str(tmp_path / "buckets.json"),
                            "--markdown-output",
                            str(tmp_path / "buckets.md"),
                        ]
                    )

    run_buckets.assert_called_once()
    assert run_buckets.call_args.args[0].clustering_mode == "ast"


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
    config = replace(synthetic_pipeline_config_fixture, clustering_mode="ast")
    records = record_refactor_buckets(
        config,
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


def test_record_refactor_buckets_allocates_against_semantic_helper_names(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Record and apply paths must share the semantic-helper existing-name set."""
    graph, bindings = inter_cluster_cycle_graph()
    config = replace(synthetic_pipeline_config_fixture, clustering_mode="ast")
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(
        "def family_b(ctx):\n    return 0.0\ndef not_semantic(x):\n    return x\n",
        encoding="utf-8",
    )
    seen_existing: list[frozenset[str]] = []

    def fake_allocate(
        unit_members: Sequence[Sequence[str]],
        address_to_series_id: Mapping[str, str],
        *,
        existing_names: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        _ = address_to_series_id
        seen_existing.append(existing_names)
        return tuple(f"helper_{index}" for index in range(len(unit_members)))

    with (
        patch(
            "src.record_refactor_buckets.allocate_schedule_helper_names",
            side_effect=fake_allocate,
        ),
        patch(
            "src.record_refactor_buckets.build_singleton_refactor_context",
            return_value=None,
        ),
        patch(
            "src.record_refactor_buckets.build_cluster_refactor_context",
            return_value=None,
        ),
    ):
        record_refactor_buckets(
            config,
            graph=graph,
            internals_path=internals_path,
            refactor_graph=cast(ProjectionResult, graph),
            internal_binding_index=None,
            layout=None,
            compression="optimal",
            bound_address_keys=bindings,
            address_to_series_id={
                "Engine!B2": "family_b",
                "Engine!C2": "family_c",
                "Engine!B3": "family_b",
                "Engine!C3": "family_c",
            },
        )

    assert seen_existing == [frozenset({"family_b"})]
