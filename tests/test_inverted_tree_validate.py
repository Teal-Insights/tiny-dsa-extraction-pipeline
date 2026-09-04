"""Default-path FormulaEvaluator canary for inverted-tree export."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.inverted_tree_validate import write_formula_evaluator_parity_reports
from src.pipeline_config import (
    DistProjectMetadata,
    InvertedTreeValidateCase,
    PipelineConfig,
)


def _sample_config(
    repo_root: Path,
    *,
    cases: tuple[InvertedTreeValidateCase, ...] = (),
) -> PipelineConfig:
    workbook = repo_root / "data" / "workbook.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"workbook")
    guide = repo_root / "data" / "guide.md"
    guide.write_text("guide\n", encoding="utf-8")
    bindings = repo_root / "bindings"
    bindings.mkdir(parents=True, exist_ok=True)
    return PipelineConfig(
        repo_root=repo_root,
        workbook_path=workbook,
        guide_path=guide,
        bindings_path=bindings,
        dist_root=repo_root / "dist",
        targets=("Sheet!A1",),
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
        inverted_tree_validate_cases=cases,
    )


def test_write_formula_evaluator_parity_reports_fails_closed_on_empty_cases(
    tmp_path: Path,
) -> None:
    config = _sample_config(tmp_path)
    with pytest.raises(RuntimeError, match="INVERTED_TREE_VALIDATE_CASES"):
        write_formula_evaluator_parity_reports(config, report_dir=tmp_path / "reports")


def test_write_formula_evaluator_parity_reports_fails_closed_on_empty_addresses(
    tmp_path: Path,
) -> None:
    config = _sample_config(
        tmp_path,
        cases=(
            InvertedTreeValidateCase(
                compute_name="compute_output_baseline",
                addresses=(),
                data_kwargs=(("country_name", "COUNTRY_NAME_DEFAULT"),),
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="no output addresses"):
        write_formula_evaluator_parity_reports(config, report_dir=tmp_path / "reports")


def test_write_formula_evaluator_parity_reports_compares_configured_computes(
    tmp_path: Path,
) -> None:
    config = _sample_config(
        tmp_path,
        cases=(
            InvertedTreeValidateCase(
                compute_name="compute_output_baseline",
                addresses=("Outputs!B12", "Outputs!C12"),
                data_kwargs=(("country_name", "COUNTRY_NAME_DEFAULT"),),
            ),
        ),
    )
    package = SimpleNamespace(
        compute_output_baseline=MagicMock(return_value=(1.0, 2.0)),
        data=SimpleNamespace(COUNTRY_NAME_DEFAULT="Borvelia"),
    )
    graph = MagicMock()
    evaluator = MagicMock()
    evaluator.evaluate.return_value = {
        "Outputs!B12": 1.0,
        "Outputs!C12": 2.0,
    }
    report_dir = tmp_path / "reports"

    with (
        patch("src.inverted_tree_validate._load_package", return_value=package),
        patch(
            "src.inverted_tree_validate.FormulaEvaluator",
            return_value=evaluator,
        ),
    ):
        exit_code = write_formula_evaluator_parity_reports(
            config,
            report_dir=report_dir,
            graph=graph,
        )

    assert exit_code == 0
    package.compute_output_baseline.assert_called_once_with(country_name="Borvelia")
    evaluator.evaluate.assert_called_once_with(["Outputs!B12", "Outputs!C12"])
    report = (report_dir / "parity_report.txt").read_text(encoding="utf-8")
    assert "FormulaEvaluator" in report
    assert "Result:            PASS" in report
