"""Copy differential validation assets into the exported dist project."""

from __future__ import annotations

import shutil
from pathlib import Path

from src.pipeline_config import PipelineConfig

HARNESS_SOURCE = Path("tests/differential/differential_test_exported_library.py")
REFERENCE_REPORT_FILES = ("parity_report.csv", "parity_report.txt")


def _render_tests_readme(*, package_name: str, library_name: str) -> str:
    return f"""# Excel parity validation

This folder ships the exported-library differential test, the workbook fixture,
and reference parity reports produced in a maintainer Windows environment with
Microsoft Excel installed.

## Reference results

`results/reference/` contains the last committed parity report from the
extraction pipeline. These reports document that the exported `{package_name}`
package matched Excel for the configured scenario sweep.

## Re-run locally (Windows + Excel only)

The test drives Excel through `xlwings` and cannot run in Linux CI.

```pwsh
uv run --project . --group validation python tests/differential_test_exported_library.py --layout exported
```

Local reruns write to `results/local/` by default. To refresh the shipped
reference reports after a passing run:

```pwsh
uv run --project . --group validation python tests/differential_test_exported_library.py --layout exported --report-dir tests/results/reference
```
"""


def export_validation_assets(*, config: PipelineConfig) -> None:
    """Copy harness, workbook fixture, and reference reports into ``dist/tests/``."""
    repo_root = config.repo_root
    dist_root = config.dist_root
    tests_root = dist_root / "tests"
    fixtures_root = tests_root / "fixtures"
    reference_root = tests_root / "results" / "reference"
    local_root = tests_root / "results" / "local"

    harness_src = repo_root / HARNESS_SOURCE
    workbook_src = repo_root / config.differential_workbook_rel
    report_src = repo_root / config.differential_report_dir_rel
    workbook_fixture_name = workbook_src.name

    if not harness_src.is_file():
        raise FileNotFoundError(f"Differential harness not found: {harness_src}")
    if not workbook_src.is_file():
        raise FileNotFoundError(f"Workbook not found: {workbook_src}")

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

    fixtures_root.mkdir(parents=True, exist_ok=True)
    reference_root.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(harness_src, tests_root / "differential_test_exported_library.py")
    shutil.copy2(workbook_src, fixtures_root / workbook_fixture_name)
    for name in REFERENCE_REPORT_FILES:
        shutil.copy2(report_src / name, reference_root / name)

    (tests_root / "README.md").write_text(
        _render_tests_readme(
            package_name=config.dist_metadata.package_name,
            library_name=config.dist_metadata.library_name,
        ),
        encoding="utf-8",
    )
