"""Post-refactor exported-library differential orchestration."""

from __future__ import annotations

import logging
import os

from src.pipeline_config import PipelineConfig

logger = logging.getLogger(__name__)


def running_in_ci() -> bool:
    """Return True when running under GitHub Actions / standard CI envs."""
    return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


def run_post_refactor_differential(*, config: PipelineConfig) -> int | None:
    """Run the exported-library differential against the post-refactor package.

    Writes reports under ``config.differential_report_dir_rel`` in the extraction
    repo. Skips under CI so committed caches can be reused. Non-zero exits and
    harness exceptions are logged as warnings; they do not abort the pipeline.
    """
    if running_in_ci():
        logger.warning(
            "Skipping exported-library differential in CI; "
            "reusing cached reports under %s",
            config.differential_report_dir_rel.as_posix(),
        )
        return None

    from tests.differential.differential_test_exported_library import (
        resolve_config,
        run_differential_test,
    )

    harness_path = (
        config.repo_root
        / "tests"
        / "differential"
        / "differential_test_exported_library.py"
    )
    report_dir = config.repo_root / config.differential_report_dir_rel
    try:
        differential_config = resolve_config(
            module_path=harness_path,
            layout="repo",
            report_dir=report_dir,
        )
        exit_code = run_differential_test(differential_config)
    except Exception:
        logger.exception(
            "Exported-library differential failed with an unhandled exception; "
            "continuing so any existing reports can still be exported"
        )
        return None

    if exit_code != 0:
        logger.warning(
            "Exported-library differential finished with exit code %s; "
            "exporting reports under %s for diagnosis",
            exit_code,
            report_dir,
        )
    return exit_code
