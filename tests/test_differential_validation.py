"""Tests for post-refactor exported-library differential orchestration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.differential_validation import (
    PARITY_CACHE_META_FILENAME,
    REFERENCE_REPORT_FILES,
    can_reuse_cached_differential_reports,
    differential_cache_key,
    run_post_refactor_differential,
    running_in_ci,
    save_differential_cache_meta,
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
        binding_authoring_prompt_path=repo_root
        / "templates"
        / "binding-authoring-prompt.txt",
        user_guide_agent_prompt_path=repo_root / "templates" / "user-guide-agent.txt",
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=repo_root / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
    )


def _seed_fingerprint_inputs(config: PipelineConfig) -> Path:
    """Create minimal package, workbook, and harness files for fingerprinting."""
    package_root = config.package_root
    package_root.mkdir(parents=True)
    (package_root / "api.py").write_text(
        "def compute():\n    return 1\n", encoding="utf-8"
    )
    (package_root / "data.py").write_text("VALUE = 1\n", encoding="utf-8")

    config.workbook_path.parent.mkdir(parents=True, exist_ok=True)
    config.workbook_path.write_bytes(b"fake-workbook")

    harness_dir = config.repo_root / "tests" / "differential"
    harness_dir.mkdir(parents=True)
    for name in (
        "differential_test_exported_library.py",
        "differential_types.py",
        "differential_excel.py",
        "comparison_utils.py",
        "workbook_labels.py",
        "differential_scenario_inputs.py",
        "binding_adapter.py",
    ):
        (harness_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    return harness_dir


def _write_parity_reports(report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "parity_report.csv").write_text("scenario_id\n", encoding="utf-8")
    (report_dir / "parity_report.txt").write_text("PASS\n", encoding="utf-8")


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
    _seed_fingerprint_inputs(config)
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
    _seed_fingerprint_inputs(config)

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
    _seed_fingerprint_inputs(config)

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


def test_differential_cache_key_changes_when_package_changes(tmp_path: Path) -> None:
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    first = differential_cache_key(config=config)
    (config.package_root / "api.py").write_text(
        "def compute():\n    return 2\n", encoding="utf-8"
    )
    second = differential_cache_key(config=config)
    assert first != second


def test_differential_cache_key_changes_when_workbook_changes(tmp_path: Path) -> None:
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    first = differential_cache_key(config=config)
    config.workbook_path.write_bytes(b"workbook-v2")
    second = differential_cache_key(config=config)
    assert first != second


@pytest.mark.parametrize(
    "harness_file",
    [
        "differential_test_exported_library.py",
        "differential_types.py",
        "differential_excel.py",
        "comparison_utils.py",
        "workbook_labels.py",
        "differential_scenario_inputs.py",
        "binding_adapter.py",
    ],
)
def test_differential_cache_key_changes_when_harness_changes(
    tmp_path: Path, harness_file: str
) -> None:
    config = _sample_config(tmp_path / "repo")
    harness_dir = _seed_fingerprint_inputs(config)
    first = differential_cache_key(config=config)
    (harness_dir / harness_file).write_text(
        f"# {harness_file} changed\n", encoding="utf-8"
    )
    second = differential_cache_key(config=config)
    assert first != second


def test_differential_cache_key_changes_when_excel_grapher_version_changes(
    tmp_path: Path,
) -> None:
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    with patch("src.differential_validation.version", return_value="1.0.0"):
        first = differential_cache_key(config=config)
    with patch("src.differential_validation.version", return_value="1.0.1"):
        second = differential_cache_key(config=config)
    assert first != second


def test_can_reuse_cached_reports_requires_successful_meta_and_files(
    tmp_path: Path,
) -> None:
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    report_dir = config.repo_root / config.differential_report_dir_rel
    cache_key = differential_cache_key(config=config)

    assert not can_reuse_cached_differential_reports(
        report_dir=report_dir, cache_key=cache_key
    )

    _write_parity_reports(report_dir)
    assert not can_reuse_cached_differential_reports(
        report_dir=report_dir, cache_key=cache_key
    )

    save_differential_cache_meta(
        report_dir=report_dir, cache_key=cache_key, exit_code=1
    )
    assert not can_reuse_cached_differential_reports(
        report_dir=report_dir, cache_key=cache_key
    )

    save_differential_cache_meta(
        report_dir=report_dir, cache_key=cache_key, exit_code=0
    )
    assert can_reuse_cached_differential_reports(
        report_dir=report_dir, cache_key=cache_key
    )

    assert not can_reuse_cached_differential_reports(
        report_dir=report_dir, cache_key="other-key"
    )


def test_run_post_refactor_differential_reuses_cache_on_hit(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    report_dir = config.repo_root / config.differential_report_dir_rel
    cache_key = differential_cache_key(config=config)
    _write_parity_reports(report_dir)
    save_differential_cache_meta(
        report_dir=report_dir, cache_key=cache_key, exit_code=0
    )

    with (
        patch(f"{_HARNESS}.run_differential_test") as run_test,
        caplog.at_level(logging.INFO),
    ):
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code == 0
    run_test.assert_not_called()
    assert "Reusing cached exported-library differential" in caplog.text


def test_run_post_refactor_differential_reruns_on_fingerprint_miss(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    report_dir = config.repo_root / config.differential_report_dir_rel
    _write_parity_reports(report_dir)
    save_differential_cache_meta(
        report_dir=report_dir, cache_key="stale-key", exit_code=0
    )

    def _fake_run(_cfg: object) -> int:
        _write_parity_reports(report_dir)
        return 0

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(f"{_HARNESS}.run_differential_test", side_effect=_fake_run) as run_test,
    ):
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code == 0
    run_test.assert_called_once()
    meta = json.loads(
        (report_dir / PARITY_CACHE_META_FILENAME).read_text(encoding="utf-8")
    )
    assert meta["cache_key"] == differential_cache_key(config=config)
    assert meta["exit_code"] == 0
    for name in REFERENCE_REPORT_FILES:
        assert (report_dir / name).is_file()


def test_run_post_refactor_differential_no_cache_bypasses_hit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    report_dir = config.repo_root / config.differential_report_dir_rel
    cache_key = differential_cache_key(config=config)
    _write_parity_reports(report_dir)
    save_differential_cache_meta(
        report_dir=report_dir, cache_key=cache_key, exit_code=0
    )

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(f"{_HARNESS}.run_differential_test", return_value=0) as run_test,
    ):
        exit_code = run_post_refactor_differential(config=config, no_cache=True)

    assert exit_code == 0
    run_test.assert_called_once()


def test_run_post_refactor_differential_records_failed_exit_without_reuse(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    report_dir = config.repo_root / config.differential_report_dir_rel

    def _fake_run(_cfg: object) -> int:
        _write_parity_reports(report_dir)
        return 1

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(f"{_HARNESS}.run_differential_test", side_effect=_fake_run),
    ):
        exit_code = run_post_refactor_differential(config=config)

    assert exit_code == 1
    meta_path = report_dir / PARITY_CACHE_META_FILENAME
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["exit_code"] == 1
    assert not can_reuse_cached_differential_reports(
        report_dir=report_dir,
        cache_key=differential_cache_key(config=config),
    )


def test_run_post_refactor_differential_invalidates_cache_on_exception(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    config = _sample_config(tmp_path / "repo")
    _seed_fingerprint_inputs(config)
    report_dir = config.repo_root / config.differential_report_dir_rel
    cache_key = differential_cache_key(config=config)
    _write_parity_reports(report_dir)
    save_differential_cache_meta(
        report_dir=report_dir, cache_key=cache_key, exit_code=0
    )

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(
            f"{_HARNESS}.run_differential_test",
            side_effect=RuntimeError("excel unavailable"),
        ),
    ):
        exit_code = run_post_refactor_differential(config=config, no_cache=True)

    assert exit_code is None
    assert not can_reuse_cached_differential_reports(
        report_dir=report_dir, cache_key=cache_key
    )

    with (
        patch(f"{_HARNESS}.resolve_config", return_value=MagicMock()),
        patch(f"{_HARNESS}.run_differential_test", return_value=0) as run_test,
    ):
        retry_exit = run_post_refactor_differential(config=config)

    assert retry_exit == 0
    run_test.assert_called_once()
