"""Lock-in tests that authored docs match inverted-tree pipeline stages."""

from __future__ import annotations

from pathlib import Path

from src.extraction_pipeline import PIPELINE_STAGES
from src.package_materialize import PackageCacheKeys

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_stages_are_inverted_tree_order() -> None:
    assert PIPELINE_STAGES == (
        "extract",
        "export",
        "annotate",
        "validate",
        "document",
    )


def test_readme_describes_live_stages_not_ctx_refactor() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "--start-from-stage annotate" in text
    assert "--start-from-stage refactor" not in text
    assert "annotate[Annotate]" in text
    assert "refactor[Refactor]" not in text
    assert "records-shaped API" not in text
    assert "keyword-only" in text
    assert "compute_*" in text
    assert "FormulaEvaluator" in text
    for stage in PIPELINE_STAGES:
        assert stage in text


def test_readme_does_not_require_cluster_diagnostics_before_export() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert (
        "Do not run the full pipeline for the first export until compare has been run"
        not in text
    )
    assert "Cluster diagnostics" not in text.split("## Pipeline stages", 1)[0]
    assert "Leftover clustering and refactor" not in text
    assert "compare_cluster_variation_modes" not in text
    assert "run_refactor_stage" not in text
    assert "run_semantic_naming" not in text


def test_pyproject_description_is_not_placeholder() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'description = "Add your description here"' not in text
    assert "inverted-tree" in text.lower() or "extraction" in text.lower()


def test_data_readme_names_tiny_dsa_inputs() -> None:
    text = (REPO_ROOT / "data" / "README.md").read_text(encoding="utf-8")
    assert "tiny-dsa.xlsx" in text
    assert "tiny-dsa-guide.md" in text
    assert "workbook.xlsx" not in text
    assert "guide.md" not in text.replace("tiny-dsa-guide.md", "")


def test_workbook_config_header_lists_live_stages() -> None:
    header = "\n".join(
        (REPO_ROOT / "workbook_config.py").read_text(encoding="utf-8").splitlines()[:6]
    )
    assert "annotate" in header
    assert "extract → export → test → document → refactor" not in header


def test_artifacts_docs_list_annotate_not_live_refactor() -> None:
    readme = (REPO_ROOT / "artifacts" / "README.md").read_text(encoding="utf-8")
    catalog = (REPO_ROOT / "artifacts" / "artifacts-catalog.md").read_text(
        encoding="utf-8"
    )
    for text in (readme, catalog):
        assert "annotate.json" in text or "stages/annotate" in text
        assert "stages/refactor.json" not in text
        assert "stages/{export,refactor,validate,document}" not in text


def test_technical_standard_export_gate_is_inverted_tree() -> None:
    text = (REPO_ROOT / "technical_standard.md").read_text(encoding="utf-8")
    assert "Records-shaped public API" not in text
    assert "configure → extract → export → refactor" not in text
    assert "keyword-only" in text
    assert "FormulaEvaluator" in text
    assert "annotate" in text


def test_lessons_learned_does_not_center_library_vs_excel() -> None:
    text = (REPO_ROOT / "lessons-learned.md").read_text(encoding="utf-8")
    assert "FormulaEvaluator" in text
    assert "annotate" in text
    lowered = text.lower()
    assert "library-vs-excel" not in lowered
    assert "exported-library" not in lowered or "graph" in lowered


def test_env_example_drops_refactor_model() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DOCSTRING_MODEL=" in text
    assert "REFACTOR_MODEL" not in text
    assert "CURSOR_API_KEY=" in text
    assert "DOCUMENT_AGENT_MODEL=" in text
    assert "SECTION_REWRITE_MODEL" not in text


def test_inverted_tree_migration_names_live_path_and_grapher_floor() -> None:
    text = (REPO_ROOT / "docs" / "inverted-tree-migration.md").read_text(
        encoding="utf-8"
    )
    assert "annotate" in text
    assert "compute_*" in text
    assert "FormulaEvaluator" in text
    assert "excel-grapher>=14.4.5" in text
    assert "compare_cluster_variation_modes" not in text.split("Do not keep")[0]
    assert "run_refactor_stage" not in text.split("Do not keep")[0]
    pin_idx = text.find("3c759a4")
    assert pin_idx != -1
    window = text[max(0, pin_idx - 80) : pin_idx + 120].lower()
    assert "historical" in window or "then pinned" in window or "originally" in window


def test_canonical_api_usage_is_human_note() -> None:
    text = (REPO_ROOT / "templates" / "canonical-api-usage.md").read_text(
        encoding="utf-8"
    )
    lowered = text.lower()
    assert "human" in lowered or "not loaded" in lowered
    assert "make_context()" in text
    assert "compute_*" in text


def test_agents_md_documents_annotate_and_live_caches() -> None:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "annotate" in text.lower()
    assert "inverted-tree-docstrings" in text
    assert "Pass 1 mechanical checkpoint" not in text
    assert "**before first export**" not in text
    assert "compare_cluster_variation_modes" not in text
    assert "run_semantic_naming" not in text
    assert "run_refactor_stage" not in text
    assert ".cache/clusters/" not in text
    assert ".cache/internals/" not in text
    assert "unpack/docstring" not in text
    assert "codegen" in text
    assert "paradigm" in text


def test_workbook_config_drops_clustering_and_callback_knobs() -> None:
    text = (REPO_ROOT / "workbook_config.py").read_text(encoding="utf-8")
    assert "VARIATION_MODE" not in text
    assert "CLUSTERING_MODE" not in text
    assert "DOCSTRING_CALLBACK_NAME" not in text


def test_package_cache_keys_have_no_internals_key() -> None:
    assert "internals_key" not in PackageCacheKeys.__dataclass_fields__
    assert "codegen_key" in PackageCacheKeys.__dataclass_fields__


def test_bindings_readme_does_not_teach_ctx_setters() -> None:
    text = (REPO_ROOT / "bindings" / "README.md").read_text(encoding="utf-8")
    assert "ctx.inputs" not in text
    assert "Records inputs (`set_*`)" not in text
    assert "input / `set_*`" not in text
    assert "`compute_*` / `set_*`" not in text
    assert "keyword-only" in text
    assert "compute_*" in text
    assert "helper parameter" in text.lower()


def test_ruff_format_exclude_is_the_synthetic_guide() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'exclude = ["tests/fixtures/synthetic/guide.md"]' in text
    assert 'exclude = ["tests/fixtures/**/*.md"]' not in text


def test_dormant_ctx_refactor_stack_is_removed() -> None:
    src = REPO_ROOT / "src"
    scripts = REPO_ROOT / "scripts"
    for name in (
        "internals_refactor.py",
        "mechanical_body.py",
        "mechanical_naming.py",
        "semantic_naming.py",
        "standalone_semantic_naming.py",
        "formula_clustering.py",
        "cluster_cache.py",
        "refactor_order.py",
        "refactor_fingerprints.py",
        "refactor_contracts.py",
        "refactor_bindings.py",
        "refactor_parity_gate.py",
        "refactor_return_types.py",
        "refactor_types.py",
        "key_dispatch_synthesis.py",
        "peel_entrypoint_dispatch.py",
        "empty_if_rewrite.py",
        "record_refactor_buckets.py",
        "series_remodel_diagnostics.py",
        "subgraph_projection.py",
        "helper_memoization.py",
        "soft_error_compute_codegen.py",
        "docstring_callback.py",
        "internals_cache.py",
        "workbook_addresses.py",
        "runtime_symbols.py",
    ):
        assert not (src / name).is_file(), name
    for name in (
        "run_refactor_stage.py",
        "run_semantic_naming.py",
        "compare_cluster_variation_modes.py",
        "diagnose_schedule_atomization.py",
        "inspect_cluster.py",
    ):
        assert not (scripts / name).is_file(), name
