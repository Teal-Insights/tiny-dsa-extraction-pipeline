"""Tests for post-refactor exported-library differential orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.differential_validation import (
    run_post_refactor_differential,
    running_in_ci,
)
from src.pipeline_config import DistProjectMetadata, PipelineConfig


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


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"CI": "true"}, True),
        ({"GITHUB_ACTIONS": "true"}, True),
        ({"CI": "1"}, False),
    ],
)
def test_running_in_ci(env: dict[str, str], expected: bool, monkeypatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert running_in_ci() is expected


_HARNESS = "tests.differential.differential_test_exported_library"


def test_run_post_refactor_differential_skips_in_ci(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("CI", "true")
    config = _sample_config(tmp_path / "repo")

    with patch(f"{_HARNESS}.run_differential_test") as run_test:
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code is None
    run_test.assert_not_called()
    assert "Skipping exported-library differential in CI" in caplog.text


def test_run_post_refactor_differential_runs_and_returns_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    repo_root = tmp_path / "repo"
    config = _sample_config(repo_root)
    resolved = MagicMock()

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=resolved) as resolve,
        patch(f"{_HARNESS}.run_differential_test", return_value=0) as run_test,
    ):
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code == 0
    resolve.assert_called_once()
    assert resolve.call_args.kwargs["layout"] == "repo"
    assert resolve.call_args.kwargs["report_dir"] == (
        repo_root / config.differential_report_dir_rel
    )
    run_test.assert_called_once_with(resolved)


def test_run_post_refactor_differential_warns_on_failure_without_raising(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(f"{_HARNESS}.run_differential_test", return_value=1),
    ):
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code == 1
    assert "exit code 1" in caplog.text


def test_run_post_refactor_differential_warns_on_exception_without_raising(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(
            f"{_HARNESS}.run_differential_test",
            side_effect=RuntimeError("excel unavailable"),
        ),
    ):
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code is None
    assert "excel unavailable" in caplog.text
