from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from src.graph_cache import bindings_fingerprint, file_fingerprint, stable_json
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.stage_manifest import (
    STAGE_MANIFEST_SCHEMA_VERSION,
    StageManifest,
    StageManifestDriftError,
    StageManifestError,
    StageManifestMissingError,
    assert_manifest_fresh,
    compute_input_fingerprints,
    load_stage_manifest,
    require_upstream_manifest,
    stage_manifest_path,
    write_stage_manifest,
)


def _sample_config(repo_root: Path) -> PipelineConfig:
    workbook = repo_root / "data" / "workbook.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"workbook-bytes")
    guide = repo_root / "data" / "guide.md"
    guide.write_text("guide\n", encoding="utf-8")
    bindings = repo_root / "bindings"
    bindings.mkdir(parents=True, exist_ok=True)
    (bindings / "inputs.bindings.yaml").write_text("series: []\n", encoding="utf-8")
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=workbook,
        guide_path=guide,
        bindings_path=bindings,
        dist_root=repo_root / "dist",
        targets=("Sheet!A1",),
        constraints={"Sheet!A1": float},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
        docstring_callback_name="series_docs",
        canonical_api_example_path=repo_root / "templates" / "canonical-api-usage.md",
        binding_authoring_prompt_path=repo_root
        / "templates"
        / "binding-authoring-prompt.txt",
        section_rewrite_introduction_focus_path=(
            repo_root / "templates" / "section-rewrite-introduction-focus.txt"
        ),
        section_rewrite_functional_overview_focus_path=(
            repo_root / "templates" / "section-rewrite-functional-overview-focus.txt"
        ),
        section_rewrite_illustrative_example_focus_path=(
            repo_root / "templates" / "section-rewrite-illustrative-example-focus.txt"
        ),
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=repo_root / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
        variation_mode="independent",
        clustering_mode="series_ast",
    )


def test_stage_manifest_path(tmp_path: Path) -> None:
    assert stage_manifest_path(tmp_path, "export") == (
        tmp_path / "artifacts" / "stages" / "export.json"
    )


def test_compute_input_fingerprints_labels(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    fingerprints = compute_input_fingerprints(config)

    assert fingerprints["workbook"] == file_fingerprint(config.workbook_path)
    assert fingerprints["bindings"] == bindings_fingerprint(config.bindings_path)
    assert (
        fingerprints["constraints"]
        == hashlib.sha256(stable_json(dict(config.constraints)).encode()).hexdigest()
    )
    assert (
        fingerprints["targets"]
        == hashlib.sha256(stable_json(sorted(config.targets)).encode()).hexdigest()
    )
    assert fingerprints["variation_mode"] == "independent"
    assert fingerprints["clustering_mode"] == "series_ast"
    assert "excel_grapher_version" in fingerprints
    assert (
        fingerprints["guide"]
        == hashlib.sha256(config.guide_path.read_bytes()).hexdigest()
    )


def test_load_stage_manifest_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    path = stage_manifest_path(tmp_path, "export")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        '  "stage": "export",\n'
        '  "schema_version": "0.0.0",\n'
        '  "cache_keys": {"codegen_cache_key": "' + ("c" * 64) + '"},\n'
        '  "upstream_keys": {},\n'
        '  "fingerprints": {"workbook": "abc"}\n'
        "}\n",
        encoding="utf-8",
    )

    with pytest.raises((ValueError, StageManifestError), match="schema_version"):
        load_stage_manifest(tmp_path, "export")


def test_write_and_load_stage_manifest_round_trip(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    fingerprints = compute_input_fingerprints(config)
    written = write_stage_manifest(
        config,
        stage="export",
        cache_keys={
            "graph_cache_key": "g" * 64,
            "projection_cache_key": "p" * 64,
            "codegen_cache_key": "c" * 64,
        },
        upstream_keys={"graph_cache_key": "g" * 64},
        fingerprints=fingerprints,
    )

    loaded = load_stage_manifest(tmp_path, "export")
    assert loaded == written
    assert loaded.stage == "export"
    assert loaded.schema_version == STAGE_MANIFEST_SCHEMA_VERSION
    assert loaded.cache_keys["codegen_cache_key"] == "c" * 64
    assert loaded.upstream_keys["graph_cache_key"] == "g" * 64
    assert loaded.fingerprints["workbook"] == fingerprints["workbook"]


def test_assert_manifest_fresh_passes_when_inputs_unchanged(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    manifest = write_stage_manifest(
        config,
        stage="extract",
        cache_keys={"graph_cache_key": "g" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    assert_manifest_fresh(manifest, config)


def test_assert_manifest_fresh_names_drifted_workbook(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    manifest = write_stage_manifest(
        config,
        stage="extract",
        cache_keys={"graph_cache_key": "g" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    config.workbook_path.write_bytes(b"changed-workbook")

    with pytest.raises(StageManifestDriftError, match="workbook"):
        assert_manifest_fresh(manifest, config)


def test_assert_manifest_fresh_names_drifted_bindings(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    manifest = write_stage_manifest(
        config,
        stage="extract",
        cache_keys={"graph_cache_key": "g" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    (config.bindings_path / "inputs.bindings.yaml").write_text(
        "series: [changed]\n", encoding="utf-8"
    )

    with pytest.raises(StageManifestDriftError, match="bindings"):
        assert_manifest_fresh(manifest, config)


def test_assert_manifest_fresh_names_drifted_constraints(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    manifest = write_stage_manifest(
        config,
        stage="extract",
        cache_keys={"graph_cache_key": "g" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    drifted = replace(config, constraints={"Sheet!A1": int})

    with pytest.raises(StageManifestDriftError, match="constraints"):
        assert_manifest_fresh(manifest, drifted)


def test_assert_manifest_fresh_rejects_manifest_missing_a_newer_fingerprint(
    tmp_path: Path,
) -> None:
    """A manifest written before a fingerprint label existed cannot be verified.

    Iterating only the stored labels would pass this manifest and silently skip
    the new input. The label set is a separate axis from
    ``STAGE_MANIFEST_SCHEMA_VERSION``, so this must not depend on a version bump.
    """
    config = _sample_config(tmp_path)
    full = compute_input_fingerprints(config)
    assert "variation_mode" in full
    older = {name: value for name, value in full.items() if name != "variation_mode"}
    manifest = write_stage_manifest(
        config,
        stage="extract",
        cache_keys={"graph_cache_key": "g" * 64},
        upstream_keys={},
        fingerprints=older,
    )

    with pytest.raises(StageManifestDriftError, match="variation_mode"):
        assert_manifest_fresh(manifest, config)


def test_assert_manifest_fresh_passes_when_label_sets_match(tmp_path: Path) -> None:
    """The symmetric check must not reject an ordinary up-to-date manifest."""
    config = _sample_config(tmp_path)
    manifest = write_stage_manifest(
        config,
        stage="extract",
        cache_keys={"graph_cache_key": "g" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )

    assert_manifest_fresh(manifest, config)


def test_load_stage_manifest_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(StageManifestMissingError, match="export"):
        load_stage_manifest(tmp_path, "export")


def test_require_upstream_manifest_loads_prior_stage(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    write_stage_manifest(
        config,
        stage="export",
        cache_keys={"codegen_cache_key": "c" * 64},
        upstream_keys={},
        fingerprints=compute_input_fingerprints(config),
    )
    upstream = require_upstream_manifest(config, start_from_stage="refactor")
    assert isinstance(upstream, StageManifest)
    assert upstream.stage == "export"


def test_require_upstream_manifest_missing_names_file(tmp_path: Path) -> None:
    config = _sample_config(tmp_path)
    with pytest.raises(StageManifestMissingError, match="export\\.json"):
        require_upstream_manifest(config, start_from_stage="refactor")
