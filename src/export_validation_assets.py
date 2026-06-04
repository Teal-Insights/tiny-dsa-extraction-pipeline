"""Copy differential validation assets into the exported dist project."""

from __future__ import annotations

import shutil
from pathlib import Path

HARNESS_SOURCE = Path("tests/differential/differential_test_exported_library.py")
WORKBOOK_SOURCE = Path("data/tiny-dsa.xlsx")
REFERENCE_REPORT_DIR = Path("data/differential/exported_library")

REFERENCE_REPORT_FILES = ("parity_report.csv", "parity_report.txt")


def _render_tests_readme() -> str:
    return """# Excel parity validation

This folder ships the exported-library differential test, the illustrative
workbook fixture, and reference parity reports produced in a maintainer
Windows environment with Microsoft Excel installed.

## Reference results

`results/reference/` contains the last committed parity report from the
extraction pipeline. These reports document that the exported `tiny_dsa`
package matched Excel across 118 scenarios and 15 output cells at
`atol = 1e-6`.

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


def export_validation_assets(*, repo_root: Path, dist_root: Path) -> None:
    """Copy harness, workbook fixture, and reference reports into ``dist/tests/``."""
    tests_root = dist_root / "tests"
    fixtures_root = tests_root / "fixtures"
    reference_root = tests_root / "results" / "reference"
    local_root = tests_root / "results" / "local"

    harness_src = repo_root / HARNESS_SOURCE
    workbook_src = repo_root / WORKBOOK_SOURCE
    report_src = repo_root / REFERENCE_REPORT_DIR

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
            "and commit refreshed reports under data/differential/exported_library/."
        )

    fixtures_root.mkdir(parents=True, exist_ok=True)
    reference_root.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(harness_src, tests_root / "differential_test_exported_library.py")
    shutil.copy2(workbook_src, fixtures_root / "tiny-dsa.xlsx")
    for name in REFERENCE_REPORT_FILES:
        shutil.copy2(report_src / name, reference_root / name)

    (tests_root / "README.md").write_text(_render_tests_readme(), encoding="utf-8")
