from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pipeline_monitor import StageTimer
from src.stage_timings import (
    CACHE_NAMES,
    STAGE_TIMINGS_SCHEMA_VERSION,
    PipelineTimings,
    record_cache_result,
    stage_span,
    stage_timings_path,
)


class _FakeCacheResult:
    def __init__(self, *, cache_hit: bool) -> None:
        self.cache_key = "abc123def456"
        self.cache_hit = cache_hit
        self.elapsed_seconds = 0.25


def test_stage_timings_path_is_repo_artifacts_json(tmp_path: Path) -> None:
    assert stage_timings_path(tmp_path) == tmp_path / "artifacts" / "stage-timings.json"


def test_stage_context_records_wall_clock_and_spans() -> None:
    timings = PipelineTimings()

    with timings.stage("refactor") as timer:
        timer.record("cluster_graph_formulas", 1.5)
        timer.record("pass1_apply", 2.5)

    assert [record.name for record in timings.stages] == ["refactor"]
    record = timings.stages[0]
    assert record.elapsed_seconds >= 0.0
    assert record.spans == {"cluster_graph_formulas": 1.5, "pass1_apply": 2.5}


def test_record_stage_accepts_externally_measured_timer() -> None:
    timings = PipelineTimings()
    timer = StageTimer()
    timer.record("create_dependency_graph", 3.0)

    timings.record_stage("extract", elapsed_seconds=4.0, spans=timer.as_dict())

    assert timings.as_dict()["stages"] == [
        {
            "name": "extract",
            "elapsed_seconds": 4.0,
            "spans": {"create_dependency_graph": 3.0},
        }
    ]


def test_as_dict_reports_every_cache_with_unobserved_nulls() -> None:
    timings = PipelineTimings()
    record_cache_result(timings, "dependency-graph", _FakeCacheResult(cache_hit=True))
    record_cache_result(timings, "codegen", _FakeCacheResult(cache_hit=False))

    caches = timings.as_dict()["caches"]

    assert set(caches) == set(CACHE_NAMES)
    assert caches["dependency-graph"] == {
        "cache_hit": True,
        "elapsed_seconds": 0.25,
        "cache_key": "abc123def456",
    }
    assert caches["codegen"]["cache_hit"] is False
    assert caches["projection"] == {
        "cache_hit": None,
        "elapsed_seconds": None,
        "cache_key": None,
    }


def test_record_cache_rejects_unknown_cache_name() -> None:
    timings = PipelineTimings()

    with pytest.raises(ValueError, match="unknown cache"):
        timings.record_cache(
            "not-a-cache",
            cache_hit=True,
            elapsed_seconds=0.0,
            cache_key="k",
        )


def test_record_cache_result_is_a_no_op_without_timings() -> None:
    record_cache_result(None, "projection", _FakeCacheResult(cache_hit=True))


def test_write_emits_schema_versioned_json(tmp_path: Path) -> None:
    timings = PipelineTimings()
    timings.record_stage("export", elapsed_seconds=1.0, spans={"derive_series": 0.5})
    record_cache_result(timings, "projection", _FakeCacheResult(cache_hit=True))
    path = tmp_path / "nested" / "stage-timings.json"

    timings.write(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == STAGE_TIMINGS_SCHEMA_VERSION
    assert payload["total_seconds"] == 1.0
    assert payload["stages"][0]["name"] == "export"
    assert payload["caches"]["projection"]["cache_hit"] is True


def test_stage_records_flush_to_output_path_incrementally(tmp_path: Path) -> None:
    path = tmp_path / "stage-timings.json"
    timings = PipelineTimings(output_path=path)

    with timings.stage("export"):
        pass

    assert [stage["name"] for stage in json.loads(path.read_text())["stages"]] == [
        "export"
    ]

    with timings.stage("refactor"):
        pass

    assert [stage["name"] for stage in json.loads(path.read_text())["stages"]] == [
        "export",
        "refactor",
    ]


def test_stage_records_wall_clock_even_when_the_stage_raises(tmp_path: Path) -> None:
    timings = PipelineTimings()

    with pytest.raises(RuntimeError), timings.stage("refactor"):
        raise RuntimeError("boom")

    assert [record.name for record in timings.stages] == ["refactor"]


def test_export_run_records_every_cache_it_reached(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """A full extract+export run observes all on-disk caches under the right stages.

    Only the codegen LLM docstring callback is stubbed; the graph, bindings,
    series, and projection caches are exercised for real.
    """
    from dataclasses import replace
    from unittest.mock import patch

    from src.extraction_pipeline import run_pipeline

    config = replace(
        synthetic_pipeline_config_fixture,
        repo_root=tmp_path,
        dist_root=tmp_path / "dist",
        graph_output_dir=tmp_path / "artifacts" / "dependency-graph",
    )
    (config.dist_root / config.dist_metadata.package_name).mkdir(parents=True)

    with (
        patch(
            "src.extraction_pipeline.configure_docstring_callback",
            return_value="series_docs",
        ),
        patch("src.extraction_pipeline.CodeGenerator") as generator_cls,
        patch("src.package_materialize.seed_validation_harness"),
    ):
        generator = generator_cls.return_value.__enter__.return_value
        generator.generate_modules.return_value = {"internals.py": "pass\n"}
        run_pipeline(config, stop_after_stage="export")

    payload = json.loads(stage_timings_path(tmp_path).read_text(encoding="utf-8"))
    assert [stage["name"] for stage in payload["stages"]] == ["extract", "export"]
    extract_spans = payload["stages"][0]["spans"]
    export_spans = payload["stages"][1]["spans"]
    assert "create_dependency_graph" in extract_spans
    assert "derive_series" not in extract_spans
    assert "load_series_bindings" in export_spans
    assert "derive_series" in export_spans
    assert "build_refactor_projection" in export_spans
    assert "codegen" in export_spans
    assert "write_export_package" in export_spans
    assert "create_dependency_graph" not in export_spans
    caches = payload["caches"]
    for name in (
        "dependency-graph",
        "bindings-validation",
        "series-resolution",
        "series-derived",
        "projection",
        "codegen",
    ):
        assert isinstance(caches[name]["cache_hit"], bool), name
        assert caches[name]["cache_key"]


def test_extract_records_stage_even_when_artifact_write_fails(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Extract uses stage_span finally, so a mid-stage crash still leaves a row."""
    from dataclasses import replace
    from unittest.mock import patch

    from src.extraction_pipeline import extract_dependency_graph
    from src.stage_timings import PipelineTimings

    config = replace(
        synthetic_pipeline_config_fixture,
        repo_root=tmp_path,
        graph_output_dir=tmp_path / "artifacts" / "dependency-graph",
    )
    timings = PipelineTimings(output_path=stage_timings_path(tmp_path))

    with (
        patch(
            "src.extraction_pipeline.write_dependency_graph_artifacts",
            side_effect=RuntimeError("artifact write failed"),
        ),
        pytest.raises(RuntimeError, match="artifact write failed"),
    ):
        extract_dependency_graph(config, timings=timings)

    assert [record.name for record in timings.stages] == ["extract"]
    payload = json.loads(stage_timings_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["stages"][0]["name"] == "extract"
    assert "create_dependency_graph" in payload["stages"][0]["spans"]


def test_stage_span_without_timings_yields_a_detached_timer() -> None:
    with stage_span(None, "refactor") as timer:
        timer.record("cluster_graph_formulas", 1.0)

    assert timer.as_dict() == {"cluster_graph_formulas": 1.0}


def test_stage_span_with_timings_records_the_stage() -> None:
    timings = PipelineTimings()

    with stage_span(timings, "validate") as timer:
        timer.record("post_refactor_differential", 2.0)

    assert timings.stages[0].name == "validate"
    assert timings.stages[0].spans == {"post_refactor_differential": 2.0}
