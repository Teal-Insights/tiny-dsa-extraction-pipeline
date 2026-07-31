"""Tests for exporting validation assets into dist/tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.export_validation_assets import (
    export_reference_reports,
    export_validation_assets,
    seed_validation_harness,
)
from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.qmd_python_validation import (
    VALIDATION_DEPENDENCY_GROUP,
    render_dist_pyproject_toml,
)


def _sample_config(repo_root: Path) -> PipelineConfig:
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=repo_root / "data" / "workbook.xlsx",
        guide_path=repo_root / "data" / "guide.md",
        bindings_path=repo_root / "bindings",
        dist_root=repo_root / "dist",
        targets=(),
        constraints={},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
        docstring_callback_name="series_docs",
        projection_layout=None,
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
    )


def _write_differential_package(repo_root: Path) -> None:
    tests_root = repo_root / "tests"
    differential_root = tests_root / "differential"
    differential_root.mkdir(parents=True)
    (tests_root / "__init__.py").write_text("", encoding="utf-8")
    (differential_root / "__init__.py").write_text("", encoding="utf-8")
    (differential_root / "differential_types.py").write_text(
        "# types\n", encoding="utf-8"
    )
    (differential_root / "differential_excel.py").write_text(
        "# excel\n", encoding="utf-8"
    )
    (differential_root / "comparison_utils.py").write_text(
        "# comparison\n", encoding="utf-8"
    )
    (differential_root / "differential_test_exported_library.py").write_text(
        "# harness\n",
        encoding="utf-8",
    )


def _seed_repo_with_workbook(repo_root: Path) -> Path:
    workbook_src = repo_root / "data" / "workbook.xlsx"
    workbook_src.parent.mkdir(parents=True)
    workbook_src.write_bytes(b"workbook")
    _write_differential_package(repo_root)
    return workbook_src


def test_render_dist_pyproject_toml_includes_validation_dependency_group() -> None:
    text = render_dist_pyproject_toml(
        dev_dependencies=["quarto>=0.1.0"],
        validation_dependencies=[
            "xlwings>=0.35.3",
            "pywin32>=311; sys_platform == 'win32'",
        ],
        metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
    )
    assert "[dependency-groups]" in text
    assert "dev = [" in text
    assert f"{VALIDATION_DEPENDENCY_GROUP} = [" in text
    assert '"xlwings>=0.35.3"' in text
    assert "pywin32" in text


def test_seed_validation_harness_copies_harness_and_workbook_without_reports(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    _seed_repo_with_workbook(repo_root)
    dist_root.mkdir()
    (dist_root / "my_model").mkdir()

    seed_validation_harness(config=_sample_config(repo_root))

    tests_root = dist_root / "tests"
    differential_root = tests_root / "differential"
    assert (tests_root / "__init__.py").exists()
    assert (differential_root / "__init__.py").exists()
    assert (differential_root / "differential_test_exported_library.py").read_text(
        encoding="utf-8"
    ) == "# harness\n"
    assert (differential_root / "differential_types.py").read_text(
        encoding="utf-8"
    ) == "# types\n"
    assert (differential_root / "differential_excel.py").read_text(
        encoding="utf-8"
    ) == "# excel\n"
    assert (differential_root / "comparison_utils.py").read_text(
        encoding="utf-8"
    ) == "# comparison\n"
    assert (tests_root / "fixtures" / "workbook.xlsx").read_bytes() == b"workbook"
    assert (tests_root / "results" / "local").is_dir()
    assert (tests_root / "results" / "reference").is_dir()
    assert not (tests_root / "results" / "reference" / "parity_report.csv").exists()
    assert (tests_root / "README.md").exists()


def test_export_reference_reports_copies_parity_reports(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    _seed_repo_with_workbook(repo_root)
    report_src = repo_root / "data" / "differential" / "exported_library"
    report_src.mkdir(parents=True)
    (report_src / "parity_report.csv").write_text("scenario_id\n", encoding="utf-8")
    (report_src / "parity_report.txt").write_text("PASS\n", encoding="utf-8")
    dist_root.mkdir()
    (dist_root / "my_model").mkdir()
    seed_validation_harness(config=_sample_config(repo_root))

    export_reference_reports(config=_sample_config(repo_root))

    reference_root = dist_root / "tests" / "results" / "reference"
    assert (reference_root / "parity_report.csv").read_text(encoding="utf-8") == (
        "scenario_id\n"
    )
    assert (reference_root / "parity_report.txt").read_text(
        encoding="utf-8"
    ) == "PASS\n"


def test_export_reference_reports_raises_when_reference_reports_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    _seed_repo_with_workbook(repo_root)
    dist_root.mkdir()
    seed_validation_harness(config=_sample_config(repo_root))

    with pytest.raises(FileNotFoundError, match="parity_report"):
        export_reference_reports(config=_sample_config(repo_root))


def test_export_validation_assets_copies_harness_workbook_and_reference_reports(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    _seed_repo_with_workbook(repo_root)
    report_src = repo_root / "data" / "differential" / "exported_library"
    report_src.mkdir(parents=True)
    (report_src / "parity_report.csv").write_text("scenario_id\n", encoding="utf-8")
    (report_src / "parity_report.txt").write_text("PASS\n", encoding="utf-8")

    dist_root.mkdir()
    (dist_root / "my_model").mkdir()

    export_validation_assets(config=_sample_config(repo_root))

    tests_root = dist_root / "tests"
    differential_root = tests_root / "differential"
    assert (tests_root / "__init__.py").exists()
    assert (differential_root / "__init__.py").exists()
    assert (differential_root / "differential_test_exported_library.py").read_text(
        encoding="utf-8"
    ) == "# harness\n"
    assert (differential_root / "differential_types.py").read_text(
        encoding="utf-8"
    ) == "# types\n"
    assert (tests_root / "fixtures" / "workbook.xlsx").read_bytes() == b"workbook"
    assert (tests_root / "results" / "reference" / "parity_report.csv").exists()
    assert (tests_root / "results" / "reference" / "parity_report.txt").exists()
    assert (tests_root / "README.md").exists()


def test_export_validation_assets_raises_when_reference_reports_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    dist_root = repo_root / "dist"
    _seed_repo_with_workbook(repo_root)
    dist_root.mkdir()

    with pytest.raises(FileNotFoundError, match="parity_report"):
        export_validation_assets(config=_sample_config(repo_root))
