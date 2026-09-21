"""Copy differential validation assets into the exported dist project."""

from __future__ import annotations

import shutil
from pathlib import Path

from src.pipeline_config import PipelineConfig

DIFFERENTIAL_PACKAGE_DIR = Path("tests/differential")
EXPORTED_DIFFERENTIAL_FILES = (
    "__init__.py",
    "differential_types.py",
    "differential_excel.py",
    "comparison_utils.py",
    "differential_scenario_inputs.py",
    "binding_adapter.py",
    "differential_test_exported_library.py",
)
REFERENCE_REPORT_FILES = ("parity_report.csv", "parity_report.txt")


def _render_tests_readme(*, package_name: str, library_name: str) -> str:
    return f"""# {library_name} graph-oracle parity validation

This folder ships the exported-library differential test, the workbook fixture,
and reference parity reports produced against excel-grapher's FormulaEvaluator.

## Reference results

`results/reference/` contains the last committed parity report from the
extraction pipeline. These reports document that the exported `{package_name}`
package matched the extraction graph for the configured scenario sweep.

## Re-run from the extraction repository

The test compares `compute_*` results (via `{{Output}}Inputs.from_defaults(...)`)
to FormulaEvaluator. It does not drive Microsoft Excel.

```pwsh
uv run python -m tests.differential.differential_test_exported_library
```

Local reruns from this exported project write to `results/local/` when the
extraction graph cache and `excel-grapher` are available:

```pwsh
uv run python -m tests.differential.differential_test_exported_library --layout exported
```
"""


def _copy_differential_package(*, repo_root: Path, tests_root: Path) -> None:
    package_src = repo_root / DIFFERENTIAL_PACKAGE_DIR
    package_dst = tests_root / "differential"
    package_dst.mkdir(parents=True, exist_ok=True)

    tests_init_src = repo_root / "tests" / "__init__.py"
    if not tests_init_src.is_file():
        raise FileNotFoundError(f"Tests package marker not found: {tests_init_src}")

    shutil.copy2(tests_init_src, tests_root / "__init__.py")
    for filename in EXPORTED_DIFFERENTIAL_FILES:
        source = package_src / filename
        if not source.is_file():
            raise FileNotFoundError(f"Differential package file not found: {source}")
        shutil.copy2(source, package_dst / filename)


def _tests_layout(config: PipelineConfig) -> tuple[Path, Path, Path, Path]:
    tests_root = config.dist_root / "tests"
    fixtures_root = tests_root / "fixtures"
    reference_root = tests_root / "results" / "reference"
    local_root = tests_root / "results" / "local"
    return tests_root, fixtures_root, reference_root, local_root


def seed_validation_harness(*, config: PipelineConfig) -> None:
    """Copy harness, workbook fixture, and empty results dirs into ``dist/tests/``."""
    repo_root = config.repo_root
    tests_root, fixtures_root, reference_root, local_root = _tests_layout(config)
    workbook_src = repo_root / config.differential_workbook_rel
    workbook_fixture_name = workbook_src.name

    if not workbook_src.is_file():
        raise FileNotFoundError(f"Workbook not found: {workbook_src}")

    fixtures_root.mkdir(parents=True, exist_ok=True)
    reference_root.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)

    stale_root_harness = tests_root / "differential_test_exported_library.py"
    stale_root_harness.unlink(missing_ok=True)

    _copy_differential_package(repo_root=repo_root, tests_root=tests_root)
    shutil.copy2(workbook_src, fixtures_root / workbook_fixture_name)

    (tests_root / "README.md").write_text(
        _render_tests_readme(
            package_name=config.dist_metadata.package_name,
            library_name=config.dist_metadata.library_name,
        ),
        encoding="utf-8",
    )


def export_reference_reports(*, config: PipelineConfig) -> None:
    """Copy parity reports from the extraction-repo cache into ``dist/tests/``."""
    report_src = config.repo_root / config.differential_report_dir_rel
    *_, reference_root, _local_root = _tests_layout(config)

    missing_reports = [
        name for name in REFERENCE_REPORT_FILES if not (report_src / name).is_file()
    ]
    if missing_reports:
        raise FileNotFoundError(
            "Reference parity reports missing from "
            f"{report_src}: {', '.join(missing_reports)}. "
            "Run the exported-library differential test in the extraction repo "
            f"and commit refreshed reports under {config.differential_report_dir_rel}."
        )

    reference_root.mkdir(parents=True, exist_ok=True)
    for name in REFERENCE_REPORT_FILES:
        shutil.copy2(report_src / name, reference_root / name)


def export_validation_assets(*, config: PipelineConfig) -> None:
    """Seed the harness and copy reference reports into ``dist/tests/``."""
    seed_validation_harness(config=config)
    export_reference_reports(config=config)
