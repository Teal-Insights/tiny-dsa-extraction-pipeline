"""Output compute helpers (series-bindings schema 1.10.0).

When an internals helper covers a published output series' leaves, the series
declares ``output.compute.helper`` so generated ``compute_*`` calls the helper
from record dims instead of ``xl_cell(address)``. The catalog-driven authoring
path must pass those blocks through verbatim, and the codegen cache must prune
the ``_output_leaves.py`` module excel-grapher emits alongside helper-backed
computes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.binding_authoring import build_binding_documents
from src.codegen_cache import OPTIONAL_GENERATED_MODULES, write_generated_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = REPO_ROOT / "templates" / "binding-catalog.example.yaml"


def test_example_catalog_uses_schema_1_10_0() -> None:
    catalog = yaml.safe_load(EXAMPLE_CATALOG.read_text(encoding="utf-8"))
    assert catalog["schema_version"] == "1.10.0"


def test_catalog_passes_output_compute_helper_through() -> None:
    catalog = {
        "schema_version": "1.10.0",
        "workbook": "workbook.xlsx",
        "outputs": {
            "series": [
                {
                    "id": "scenario_primary_expenditure_pct_gdp_hot",
                    "sheet": "Engine",
                    "data_range": "Engine!D10:F10",
                    "layout": "series",
                    "output": {
                        "compute": {
                            "name": "compute_scenario_primary_expenditure_pct_gdp",
                            "helper": {
                                "name": "scenario_primary_expenditure_pct_gdp_hot",
                                "dims": ["TIME_PERIOD"],
                            },
                        }
                    },
                    "key": ["TIME_PERIOD"],
                }
            ]
        },
    }
    documents = build_binding_documents(catalog)
    outputs = documents["outputs.bindings.yaml"]
    assert outputs["schema_version"] == "1.10.0"
    (series,) = outputs["series"]
    assert series["output"]["compute"] == {
        "name": "compute_scenario_primary_expenditure_pct_gdp",
        "helper": {
            "name": "scenario_primary_expenditure_pct_gdp_hot",
            "dims": ["TIME_PERIOD"],
        },
    }


def test_optional_generated_modules_include_output_leaves(tmp_path: Path) -> None:
    assert "_output_leaves.py" in OPTIONAL_GENERATED_MODULES
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "_output_leaves.py").write_text("stale\n", encoding="utf-8")
    write_generated_modules(package_root, {"api.py": "def api():\n    return 1\n"})
    assert not (package_root / "_output_leaves.py").is_file()
