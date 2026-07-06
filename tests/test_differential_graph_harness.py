"""Unit tests for the graph-oracle differential harness."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from excel_grapher import XlError
from excel_grapher.core.address_keys import normalize_key, parse_address


def _load_harness_module():
    return importlib.import_module("tests.differential.differential_test_graph")


def test_values_match_passes_within_atol() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(1.0000001, 1.0, atol=1e-6)
    assert match is True
    assert abs_diff is not None and abs_diff <= 1e-6
    assert rel_diff is not None
    assert note == ""


def test_values_match_fails_outside_atol() -> None:
    harness = _load_harness_module()
    match, abs_diff, _, _ = harness.values_match(1.01, 1.0, atol=1e-6)
    assert match is False
    assert abs_diff == pytest.approx(0.01)


def test_values_match_treats_matching_errors_as_pass() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(
        "#DIV/0!",
        XlError.DIV,
        atol=1e-6,
    )
    assert match is True
    assert abs_diff is None
    assert rel_diff is None
    assert "both error" in note


def test_run_differential_test_requires_scenarios(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = harness.GraphDifferentialConfig(
        repo_root=tmp_path,
        workbook_path=tmp_path / "workbook.xlsx",
        report_dir=tmp_path / "reports",
        targets=("Outputs!B1",),
        constraints={"Inputs!A1": float},
        library_name="Example",
    )
    with pytest.raises(RuntimeError, match="No differential scenarios"):
        harness.run_differential_test(config)


def test_parse_address_after_normalize_key_handles_spaced_sheet_names() -> None:
    assert parse_address(normalize_key("Discrete Risks!H2")) == ("Discrete Risks", "H2")
    assert parse_address(normalize_key("'Discrete Risks'!H2")) == (
        "Discrete Risks",
        "H2",
    )


def test_mvp_graph_driver_normalizes_sheet_names_with_spaces(tmp_path: Path) -> None:
    harness = _load_harness_module()
    canonical_key = normalize_key("Discrete Risks!H2")
    mock_graph = MagicMock()
    mock_graph.leaf_keys.return_value = [canonical_key]
    mock_graph.formula_keys.return_value = []
    mock_node = MagicMock()
    mock_node.value = 0
    mock_graph.get_node.return_value = mock_node

    with patch(
        "tests.differential.differential_test_graph.create_dependency_graph",
        return_value=mock_graph,
    ):
        driver = harness.MvpGraphDriver(
            tmp_path / "workbook.xlsx",
            targets=("Outputs!B1",),
            constraints={},
        )

    driver.set_inputs({"Discrete Risks!H2": 42})
    assert "Discrete Risks!H2" not in driver.missing_cells
    mock_graph.set_node_value.assert_called_once_with(canonical_key, 42)


def test_absent_input_preflight_uses_normalized_keys() -> None:
    canonical_key = normalize_key("Discrete Risks!H2")
    known_keys = frozenset({canonical_key})
    all_input_cells = frozenset({"Discrete Risks!H2", "Missing!A1"})

    missing = sorted(
        cell for cell in all_input_cells if normalize_key(cell) not in known_keys
    )
    assert missing == ["Missing!A1"]
