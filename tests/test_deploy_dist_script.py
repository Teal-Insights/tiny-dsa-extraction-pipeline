"""End-to-end tests for ``scripts/deploy_dist.sh`` against local git repositories.

Deploying merges the committed dist/ tree onto the package repository's
``main``, so commits made directly in the package repository survive. It does
not dispatch a GitHub Actions workflow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_dist.sh"
BASH = shutil.which("bash")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

requires_bash = pytest.mark.skipif(BASH is None, reason="bash is required")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    return result.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class Repos:
    pipeline: Path
    remote: Path
    seed: Path


def _setup(
    tmp_path: Path,
    *,
    deploy_subject: str = "Deploy generated package from example/pipeline@aaaa",
) -> Repos:
    """Pipeline repo with committed dist/, plus a package repo that has one
    earlier deploy followed by an upstream-only commit."""
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    _git(pipeline, "init", "-q", "-b", "main")
    _write(pipeline, {"dist/pkg/mod.py": "VALUE = 1\n", "dist/README.md": "v1\n"})
    _git(pipeline, "add", ".")
    _git(pipeline, "commit", "-q", "-m", "First dist")

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    _git(seed, "checkout", "-q", "-b", "main")
    _write(seed, {"pkg/mod.py": "VALUE = 1\n", "README.md": "v1\n"})
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", deploy_subject)
    _write(seed, {"upstream.txt": "contributed upstream\n"})
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "Upstream contribution")
    _git(seed, "push", "-q", "origin", "main")
    return Repos(pipeline=pipeline, remote=remote, seed=seed)


def _commit_dist(pipeline: Path, files: dict[str, str]) -> str:
    _write(pipeline / "dist", files)
    _git(pipeline, "add", ".")
    _git(pipeline, "commit", "-q", "-m", "Regenerate dist")
    return _git(pipeline, "rev-parse", "HEAD")


def _stub_uv(tmp_path: Path, exit_code: int) -> Path:
    """Put a fake ``uv`` on PATH that records its arguments and exits with ``exit_code``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{(tmp_path / "uv-calls.txt").as_posix()}"\nexit {exit_code}\n',
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(0o755)
    return bin_dir


def _deploy(
    repos: Repos,
    *args: str,
    skip_tests: bool = True,
    path_prefix: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    env = {**os.environ, **_GIT_ENV}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    flags = ["--skip-tests"] if skip_tests else []
    return subprocess.run(
        [BASH, SCRIPT.as_posix(), "--remote", repos.remote.as_posix(), *flags, *args],
        cwd=repos.pipeline,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _remote_file(repos: Repos, relative: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"main:{relative}"],
        cwd=repos.remote,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


@requires_bash
def test_deploy_merges_new_dist_and_keeps_upstream_commits(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    sha = _commit_dist(
        repos.pipeline, {"pkg/mod.py": "VALUE = 2\n", "pkg/new.py": "X = 1\n"}
    )
    _write(repos.pipeline, {"dist/untracked.txt": "local only\n"})

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_file(repos, "pkg/mod.py") == "VALUE = 2\n"
    assert _remote_file(repos, "pkg/new.py") == "X = 1\n"
    assert _remote_file(repos, "upstream.txt") == "contributed upstream\n"
    assert _remote_file(repos, "untracked.txt") is None
    log = _git(repos.remote, "log", "--format=%s", "main")
    assert f"Deploy generated package from pipeline@{sha}" in log


@requires_bash
def test_deploy_removes_files_dropped_from_dist(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    (repos.pipeline / "dist" / "README.md").unlink()
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_file(repos, "README.md") is None
    assert _remote_file(repos, "upstream.txt") == "contributed upstream\n"


@requires_bash
def test_second_deploy_uses_previous_deploy_as_merge_base(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})
    assert _deploy(repos).returncode == 0
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 3\n"})

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_file(repos, "pkg/mod.py") == "VALUE = 3\n"
    assert _remote_file(repos, "upstream.txt") == "contributed upstream\n"


@requires_bash
def test_deploy_refuses_uncommitted_dist_changes(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")
    _write(repos.pipeline, {"dist/pkg/mod.py": "VALUE = 99\n"})

    result = _deploy(repos)

    assert result.returncode != 0
    assert "uncommitted" in result.stderr
    assert _git(repos.remote, "rev-parse", "main") == before


@requires_bash
def test_deploy_stops_on_merge_conflict_without_pushing(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    _write(repos.seed, {"pkg/mod.py": "VALUE = 'upstream'\n"})
    _git(repos.seed, "commit", "-q", "-am", "Upstream edit to a generated file")
    _git(repos.seed, "push", "-q", "origin", "main")
    before = _git(repos.remote, "rev-parse", "main")
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos)

    assert result.returncode != 0
    assert "conflict" in result.stderr.lower()
    assert "pkg/mod.py" in result.stderr
    assert _git(repos.remote, "rev-parse", "main") == before


@requires_bash
def test_deploy_dry_run_does_not_push(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run" in result.stdout.lower()
    assert _git(repos.remote, "rev-parse", "main") == before


@requires_bash
def test_deploy_with_unchanged_dist_is_a_no_op(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to deploy" in result.stdout.lower()
    assert _git(repos.remote, "rev-parse", "main") == before


@requires_bash
def test_deploy_fails_when_remote_has_no_deploy_commit(tmp_path: Path) -> None:
    repos = _setup(tmp_path, deploy_subject="Initial commit")
    before = _git(repos.remote, "rev-parse", "main")
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos)

    assert result.returncode != 0
    assert "Deploy generated package from" in result.stderr
    assert _git(repos.remote, "rev-parse", "main") == before


@requires_bash
def test_failing_package_tests_leave_remote_unchanged(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(
        repos, skip_tests=False, path_prefix=_stub_uv(tmp_path, exit_code=1)
    )

    assert result.returncode != 0
    assert "tests failed" in result.stderr
    assert "pytest" in (tmp_path / "uv-calls.txt").read_text(encoding="utf-8")
    assert _git(repos.remote, "rev-parse", "main") == before


@requires_bash
def test_passing_package_tests_allow_the_push(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(
        repos, skip_tests=False, path_prefix=_stub_uv(tmp_path, exit_code=0)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "uv-calls.txt").read_text(encoding="utf-8")
    assert "--locked" in calls
    assert "pytest" in calls
    assert _remote_file(repos, "pkg/mod.py") == "VALUE = 2\n"


@requires_bash
def test_skip_tests_bypasses_failing_package_tests(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos, path_prefix=_stub_uv(tmp_path, exit_code=1))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "uv-calls.txt").exists()
    assert _remote_file(repos, "pkg/mod.py") == "VALUE = 2\n"


def test_deploy_script_resolves_default_target_from_workbook_config() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "repository_slug" in text
    assert "load_pipeline_config" in text
    assert "Teal-Insights" not in text


def test_deploy_script_does_not_use_github_actions() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "workflow_dispatch" not in text
    assert "gh workflow" not in text
    assert "DEPLOY_TOKEN" not in text


def test_rsync_publish_script_is_gone() -> None:
    assert not (SCRIPT.parent / "publish_dist.sh").exists()
