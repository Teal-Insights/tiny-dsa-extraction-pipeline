"""Graph cells must sit inside some series data_range before derivation."""

from __future__ import annotations

from typing import cast

import pytest
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.binding_resolution_audit import audit_binding_resolutions
from src.graph_binding_coverage import (
    GraphBindingCoverageError,
    require_graph_binding_coverage,
)
from tests.conftest import SyntheticConfiguredPipeline


def _empty_bindings() -> WorkbookSeriesBindings:
    return cast(WorkbookSeriesBindings, {"schema_version": "1.22.0", "series": []})


def test_require_graph_binding_coverage_raises_with_missing_address(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    graph = synthetic_configured_pipeline.graph
    with pytest.raises(GraphBindingCoverageError) as exc_info:
        require_graph_binding_coverage(graph, _empty_bindings())

    assert "Engine!B2" in exc_info.value.unbound_cells
    message = str(exc_info.value)
    assert "Engine!B2" in message
    assert "Busiest sheets" in message


def test_find_unbound_graph_cell_bindings_reports_unbound_graph_cell(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    from src.binding_resolution_audit import find_unbound_graph_cell_bindings

    graph = synthetic_configured_pipeline.graph
    findings = find_unbound_graph_cell_bindings(
        graph,
        _empty_bindings(),
        workbook_path=synthetic_configured_pipeline.config.workbook_path,
    )
    assert len(findings) == 1
    assert findings[0].code == "unbound_graph_cell"
    assert findings[0].severity == "error"
    assert findings[0].address == "Outputs!B1"
    assert "Engine!B2" in findings[0].message
    assert "Busiest sheets" in findings[0].message


def test_audit_binding_resolutions_keeps_other_findings_with_unbound_cells(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.binding_resolution_audit as audit_mod
    from src.binding_resolution_audit import AuditFinding

    sentinel = AuditFinding(
        severity="error",
        code="sparse_label_without_fill",
        series_id="partial",
        direction="internal",
        message="blank label",
    )

    def _resolve(*_args, **_kwargs):
        return {
            "series": [
                {
                    "series_id": "partial",
                    "ok": True,
                    "issues": [],
                    "leaves": ["Engine!A1"],
                }
            ]
        }

    monkeypatch.setattr(audit_mod, "resolve_series_bindings", _resolve)
    monkeypatch.setattr(
        audit_mod,
        "findings_from_resolution",
        lambda *_args, **_kwargs: [sentinel],
    )
    bindings = cast(
        WorkbookSeriesBindings,
        {
            "schema_version": "1.22.0",
            "series": [
                {
                    "id": "partial",
                    "sheet": "Engine",
                    "data_range": "Engine!A1",
                    "internal": {},
                }
            ],
        },
    )
    report = audit_binding_resolutions(
        synthetic_configured_pipeline.graph,
        bindings,
        workbook=synthetic_configured_pipeline.config.workbook_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "unbound_graph_cell" in codes
    assert "sparse_label_without_fill" in codes
