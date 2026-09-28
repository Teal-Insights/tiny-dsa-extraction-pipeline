"""End-to-end tests for ``scripts/deploy_dist.sh`` against local git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_dist.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


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


def _setup(tmp_path: Path) -> Repos:
    """Pipeline repo with committed dist/, plus a target repo that has one
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
    _git(
        seed,
        "commit",
        "-q",
        "-m",
        "Deploy generated package from example/pipeline@aaaa",
    )
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


def _deploy(repos: Repos, *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [
            BASH,
            SCRIPT.as_posix(),
            "--remote",
            repos.remote.as_posix(),
            "--skip-tests",
            *args,
        ],
        cwd=repos.pipeline,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
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


def test_deploy_removes_files_dropped_from_dist(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    (repos.pipeline / "dist" / "README.md").unlink()
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_file(repos, "README.md") is None
    assert _remote_file(repos, "upstream.txt") == "contributed upstream\n"


def test_second_deploy_uses_previous_deploy_as_merge_base(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})
    assert _deploy(repos).returncode == 0
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 3\n"})

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_file(repos, "pkg/mod.py") == "VALUE = 3\n"
    assert _remote_file(repos, "upstream.txt") == "contributed upstream\n"


def test_deploy_refuses_uncommitted_dist_changes(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")
    _write(repos.pipeline, {"dist/pkg/mod.py": "VALUE = 99\n"})

    result = _deploy(repos)

    assert result.returncode != 0
    assert "uncommitted" in result.stderr
    assert _git(repos.remote, "rev-parse", "main") == before


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
    assert _git(repos.remote, "rev-parse", "main") == before


def test_deploy_dry_run_does_not_push(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")
    _commit_dist(repos.pipeline, {"pkg/mod.py": "VALUE = 2\n"})

    result = _deploy(repos, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run" in result.stdout.lower()
    assert _git(repos.remote, "rev-parse", "main") == before


def test_deploy_with_unchanged_dist_is_a_no_op(tmp_path: Path) -> None:
    repos = _setup(tmp_path)
    before = _git(repos.remote, "rev-parse", "main")

    result = _deploy(repos)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to deploy" in result.stdout.lower()
    assert _git(repos.remote, "rev-parse", "main") == before
