from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline_monitor import (
    StageTimer,
    _env_flag,
    _env_float,
    resolve_stall_log_path,
)


def test_stage_timer_records_durations() -> None:
    timer = StageTimer()
    with timer.stage("alpha"):
        pass
    with timer.stage("beta"):
        pass

    timings = timer.as_dict()
    assert list(timings) == ["alpha", "beta"]
    assert all(seconds >= 0 for seconds in timings.values())


def test_env_flag_truthiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINE_PROFILE", raising=False)
    assert _env_flag("PIPELINE_PROFILE") is False

    monkeypatch.setenv("PIPELINE_PROFILE", "1")
    assert _env_flag("PIPELINE_PROFILE") is True

    monkeypatch.setenv("PIPELINE_PROFILE", "0")
    assert _env_flag("PIPELINE_PROFILE") is False


def test_env_float_parses_stall_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINE_STALL_SECONDS", raising=False)
    assert _env_float("PIPELINE_STALL_SECONDS") is None

    monkeypatch.setenv("PIPELINE_STALL_SECONDS", "120")
    assert _env_float("PIPELINE_STALL_SECONDS") == 120.0


def test_resolve_stall_log_path_uses_default(tmp_path: Path) -> None:
    assert resolve_stall_log_path(tmp_path) == tmp_path / "extraction-stall.log"


def test_resolve_stall_log_path_respects_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "custom-stall.log"
    monkeypatch.setenv("PIPELINE_STALL_LOG", str(override))

    assert resolve_stall_log_path(tmp_path) == override
