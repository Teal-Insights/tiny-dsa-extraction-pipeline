"""Unit tests for the graph-oracle differential harness."""

from __future__ import annotations

import importlib
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from excel_grapher import XlError
from excel_grapher.core.address_keys import normalize_key, parse_address


def _load_harness_module():
    return importlib.import_module("tests.differential.differential_test_graph")


def test_values_match_passes_within_atol() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(
        1.0000001, 1.0, atol=1e-6, rtol=1e-12
    )
    assert match is True
    assert abs_diff is not None and abs_diff <= 1e-6
    assert rel_diff is not None
    assert note == ""


def test_values_match_fails_outside_atol() -> None:
    harness = _load_harness_module()
    match, abs_diff, _, _ = harness.values_match(1.01, 1.0, atol=1e-6, rtol=1e-12)
    assert match is False
    assert abs_diff == pytest.approx(0.01)


def test_values_match_treats_matching_errors_as_pass() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(
        "#DIV/0!",
        XlError.DIV,
        atol=1e-6,
        rtol=1e-12,
    )
    assert match is True
    assert abs_diff is None
    assert rel_diff is None
    assert "both error" in note


def test_values_match_passes_relative_branch_at_extreme_magnitude() -> None:
    """§1.1 hybrid gate: ULP-scale rounding on huge values passes via rtol."""
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(
        1.0e16, 1.0e16 + 2.0, atol=1e-6, rtol=1e-12
    )
    assert match is True
    assert abs_diff == pytest.approx(2.0)
    assert rel_diff is not None and rel_diff <= 1e-12
    assert note == ""


def test_values_match_fails_when_both_tolerance_branches_fail() -> None:
    """A genuine divergence exemplar fails the hybrid gate."""
    harness = _load_harness_module()
    match, _, rel_diff, _ = harness.values_match(
        51.137230546619406, 1075.1372305466193, atol=1e-6, rtol=1e-12
    )
    assert match is False
    assert rel_diff is not None and rel_diff > 1e-12


def test_values_match_relative_branch_never_decides_zero_golden() -> None:
    harness = _load_harness_module()
    match, _, rel_diff, _ = harness.values_match(0.0, 1e-5, atol=1e-6, rtol=1e-12)
    assert match is False
    assert rel_diff == math.inf


def test_values_match_zero_golden_passes_only_by_absolute_branch() -> None:
    harness = _load_harness_module()
    match, _, rel_diff, _ = harness.values_match(0.0, 5e-7, atol=1e-6, rtol=1e-12)
    assert match is True
    assert rel_diff == math.inf


def test_values_match_relative_branch_uses_golden_magnitude_for_negatives() -> None:
    harness = _load_harness_module()
    match, _, rel_diff, _ = harness.values_match(
        -1.0e16, -(1.0e16 + 2.0), atol=1e-6, rtol=1e-12
    )
    assert match is True
    assert rel_diff is not None and 0 < rel_diff <= 1e-12


def test_values_match_relative_branch_boundary_is_inclusive() -> None:
    harness = _load_harness_module()
    match, _, rel_diff, _ = harness.values_match(
        1.0e15, 1.0e15 + 1000.0, atol=1e-6, rtol=1e-12
    )
    assert match is True
    assert rel_diff == 1e-12


def test_values_match_decides_out_of_double_range_ints_exactly() -> None:
    harness = _load_harness_module()
    match, abs_diff, rel_diff, note = harness.values_match(
        10**400, 10**400, atol=1e-6, rtol=1e-12
    )
    assert match is True
    assert abs_diff is None and rel_diff is None
    assert "double range" in note

    mismatch, *_ = harness.values_match(10**400, 10**400 + 1, atol=1e-6, rtol=1e-12)
    assert mismatch is False


def test_graph_harness_exports_rtol_constant() -> None:
    harness = _load_harness_module()
    assert harness.RTOL == 1e-12


def test_graph_config_defaults_thread_hybrid_tolerances(tmp_path: Path) -> None:
    config = _graph_config(tmp_path)
    assert config.atol == 1e-6
    assert config.rtol == 1e-12


def test_graph_txt_summary_header_reports_both_tolerances(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _graph_config(tmp_path)
    report_path = tmp_path / "differential_report.txt"

    harness.write_txt_summary(
        [_matched_error_trial(harness)],
        report_path,
        config=config,
        missing_inputs_in_graph=[],
    )

    text = report_path.read_text(encoding="utf-8")
    assert "Tolerance: atol = 1e-06, rtol = 1e-12" in text


def _graph_config(tmp_path: Path, **overrides):
    harness = _load_harness_module()
    kwargs = {
        "repo_root": tmp_path,
        "workbook_path": tmp_path / "workbook.xlsx",
        "report_dir": tmp_path / "reports",
        "targets": ("Outputs!B1",),
        "constraints": {"Inputs!A1": float},
        "library_name": "Example",
    }
    kwargs.update(overrides)
    return harness.GraphDifferentialConfig(**kwargs)


def _matched_error_trial(harness):
    return harness.Trial(
        axis="scenarios",
        point_label="a",
        scenario_id="scenario:a",
        output_label="result[year=1]",
        cell="Outputs!B1",
        golden=XlError.NA,
        mvp=XlError.NA,
        match=True,
        abs_diff=None,
        rel_diff=None,
        note="both error: #N/A",
        matched_error=True,
        flagged_matched_error=True,
    )


def test_txt_summary_fails_on_flagged_matched_errors(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _graph_config(tmp_path)
    report_path = tmp_path / "differential_report.txt"

    harness.write_txt_summary(
        [_matched_error_trial(harness)],
        report_path,
        config=config,
        missing_inputs_in_graph=[],
    )

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            FAIL" in text
    assert "MATCHED ERROR VALUES" in text


def test_txt_summary_passes_when_matched_errors_allowed(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = _graph_config(tmp_path, allow_matched_errors=True)
    report_path = tmp_path / "differential_report.txt"

    harness.write_txt_summary(
        [_matched_error_trial(harness)],
        report_path,
        config=config,
        missing_inputs_in_graph=[],
    )

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            PASS" in text
    assert "MATCHED ERROR VALUES" in text


def test_csv_report_includes_flagged_matched_error_column(tmp_path: Path) -> None:
    import csv

    harness = _load_harness_module()
    report_path = tmp_path / "differential_report.csv"

    harness.write_csv_report([_matched_error_trial(harness)], report_path)

    with report_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert "flagged_matched_error" in rows[0]
    assert rows[1][rows[0].index("flagged_matched_error")] == "True"


def test_parse_args_accepts_allow_matched_errors() -> None:
    harness = _load_harness_module()
    args = harness.parse_args(["--allow-matched-errors"])
    assert args.allow_matched_errors is True
    assert harness.parse_args([]).allow_matched_errors is False


def test_parse_args_rejects_removed_warn_flag() -> None:
    harness = _load_harness_module()
    with pytest.raises(SystemExit):
        harness.parse_args(["--warn-on-error-values"])


def test_run_differential_test_requires_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate stays empty-hooks even when derived repos author a real matrix."""
    harness = _load_harness_module()
    monkeypatch.setattr(harness, "build_axes", lambda: ())
    monkeypatch.setattr(harness, "build_scenarios", lambda: ())
    monkeypatch.setattr(harness, "output_cell_labels", lambda: ())
    (tmp_path / "workbook.xlsx").write_bytes(b"stub")
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


def _mock_graph_with_leaf(canonical_key: str) -> MagicMock:
    mock_graph = MagicMock()
    mock_graph.leaf_keys.return_value = [canonical_key]
    mock_graph.formula_keys.return_value = []
    mock_node = MagicMock()
    mock_node.value = 0
    mock_graph.get_node.return_value = mock_node
    return mock_graph


def _cached_graph_result(graph: MagicMock):
    from src.graph_cache import DependencyGraphCacheResult

    return DependencyGraphCacheResult(
        graph=graph,
        cache_key="hit-key",
        cache_hit=True,
        elapsed_seconds=0.0,
    )


def test_mvp_graph_driver_normalizes_sheet_names_with_spaces(tmp_path: Path) -> None:
    harness = _load_harness_module()
    canonical_key = normalize_key("Discrete Risks!H2")
    mock_graph = _mock_graph_with_leaf(canonical_key)

    with patch(
        "tests.differential.differential_test_graph.try_load_cached_dependency_graph",
        return_value=_cached_graph_result(mock_graph),
    ):
        driver = harness.MvpGraphDriver(
            tmp_path / "workbook.xlsx",
            targets=("Outputs!B1",),
            constraints={},
        )

    driver.set_inputs({"Discrete Risks!H2": 42})
    assert "Discrete Risks!H2" not in driver.missing_cells
    mock_graph.set_node_value.assert_called_once_with(canonical_key, 42)


def test_mvp_graph_driver_prefers_committed_warm_cache(tmp_path: Path) -> None:
    """Warm committed cache hit must not cold-build or write via get_or_build."""
    from src.graph_cache import COMMITTED_GRAPH_CACHE_DIR

    harness = _load_harness_module()
    mock_graph = _mock_graph_with_leaf(normalize_key("Inputs!A1"))
    workbook = tmp_path / "workbook.xlsx"
    targets = ("Outputs!B1",)
    constraints = {"Inputs!A1": float}

    with (
        patch(
            "tests.differential.differential_test_graph.try_load_cached_dependency_graph",
            return_value=_cached_graph_result(mock_graph),
        ) as try_load,
        patch(
            "tests.differential.differential_test_graph.get_or_build_dependency_graph",
        ) as get_or_build,
        patch("src.graph_cache.create_dependency_graph") as create,
    ):
        driver = harness.MvpGraphDriver(
            workbook,
            targets=targets,
            constraints=constraints,
        )

    assert driver._graph is mock_graph
    try_load.assert_called_once()
    kwargs = try_load.call_args.kwargs
    assert kwargs["workbook_path"] == workbook
    assert kwargs["targets"] == targets
    assert kwargs["constraints"] == constraints
    assert kwargs["load_values"] is True
    assert kwargs["capture_dependency_provenance"] is True
    assert kwargs["cache_dir"] is COMMITTED_GRAPH_CACHE_DIR
    get_or_build.assert_not_called()
    create.assert_not_called()


def test_mvp_graph_driver_builds_via_cache_helper_on_committed_miss(
    tmp_path: Path,
) -> None:
    """Cache miss must use get_or_build (save), never bare create_dependency_graph."""
    from src.graph_cache import DEFAULT_GRAPH_CACHE_DIR

    harness = _load_harness_module()
    mock_graph = _mock_graph_with_leaf(normalize_key("Inputs!A1"))
    workbook = tmp_path / "workbook.xlsx"
    targets = ("Outputs!B1",)
    constraints = {"Inputs!A1": float}
    built = _cached_graph_result(mock_graph)

    with (
        patch(
            "tests.differential.differential_test_graph.try_load_cached_dependency_graph",
            return_value=None,
        ) as try_load,
        patch(
            "tests.differential.differential_test_graph.get_or_build_dependency_graph",
            return_value=built,
        ) as get_or_build,
        patch("src.graph_cache.create_dependency_graph") as create,
    ):
        driver = harness.MvpGraphDriver(
            workbook,
            targets=targets,
            constraints=constraints,
        )

    assert driver._graph is mock_graph
    try_load.assert_called_once()
    get_or_build.assert_called_once()
    kwargs = get_or_build.call_args.kwargs
    assert kwargs["workbook_path"] == workbook
    assert kwargs["targets"] == targets
    assert kwargs["constraints"] == constraints
    assert kwargs["load_values"] is True
    assert kwargs["capture_dependency_provenance"] is True
    assert (
        kwargs.get("cache_dir") is None
        or kwargs["cache_dir"] is DEFAULT_GRAPH_CACHE_DIR
    )
    assert "dynamic_refs" in kwargs
    create.assert_not_called()


def test_absent_input_preflight_uses_normalized_keys() -> None:
    canonical_key = normalize_key("Discrete Risks!H2")
    known_keys = frozenset({canonical_key})
    all_input_cells = frozenset({"Discrete Risks!H2", "Missing!A1"})

    missing = sorted(
        cell for cell in all_input_cells if normalize_key(cell) not in known_keys
    )
    assert missing == ["Missing!A1"]


class _FakeGolden:
    def __init__(self, values: dict[str, float]) -> None:
        self._values = values

    def record_input_baselines(self, cells: frozenset[str]) -> None:
        pass

    def reset_inputs(self) -> None:
        pass

    def set_inputs(self, inputs: dict[str, object]) -> None:
        pass

    def read(self, cell: str) -> object:
        return self._values[cell]

    def close(self) -> None:
        pass


class _FakeMvp:
    def __init__(self, values: dict[str, float]) -> None:
        self._values = values
        self._known_keys = frozenset(values)
        self.missing_cells: set[str] = set()

    def record_input_baselines(self, cells: frozenset[str]) -> None:
        pass

    def reset_inputs(self) -> None:
        pass

    def set_inputs(self, inputs: dict[str, object]) -> None:
        pass

    def read(self, cell: str) -> object:
        return self._values[cell]


def _two_scenario_hooks(harness):
    from tests.differential.differential_types import Axis, AxisPoint, Scenario

    first = Scenario(id="first", inputs={"value": 1.0})
    second = Scenario(id="second", inputs={"value": 2.0})
    axes = (
        Axis(
            "scenarios",
            (
                AxisPoint(label="first", scenario=first),
                AxisPoint(label="second", scenario=second),
            ),
        ),
    )
    writes = {"first": {"Inputs!A1": 1.0}, "second": {"Inputs!B1": 2.0}}
    return (
        patch.object(harness, "build_axes", return_value=axes),
        patch.object(harness, "output_cell_labels", return_value=(("out", "Out!C1"),)),
        patch.object(
            harness,
            "inputs_for_excel",
            side_effect=lambda scenario: writes[scenario.id],
        ),
    )


def _sweep_config(harness, tmp_path: Path):
    return harness.GraphDifferentialConfig(
        repo_root=tmp_path,
        workbook_path=tmp_path / "workbook.xlsx",
        report_dir=tmp_path / "reports",
        targets=("Out!C1",),
        constraints={"Inputs!A1": float},
        library_name="Example",
    )


def _run_sweep_with_output_values(
    harness, tmp_path: Path, golden_out: float, mvp_out: float
):
    golden = _FakeGolden({"Out!C1": golden_out, "Inputs!A1": 1.0, "Inputs!B1": 2.0})
    mvp = _FakeMvp({"Out!C1": mvp_out, "Inputs!A1": 1.0, "Inputs!B1": 2.0})

    hook_patches = _two_scenario_hooks(harness)
    with (
        hook_patches[0],
        hook_patches[1],
        hook_patches[2],
        patch.object(harness, "GoldenDriver", lambda path: golden),
        patch.object(harness, "MvpGraphDriver", lambda path, **kwargs: mvp),
    ):
        return harness.run_sweep(_sweep_config(harness, tmp_path))


def test_run_sweep_threads_config_rtol_to_the_gate(tmp_path: Path) -> None:
    """1-ULP noise at 1e16 (abs_diff=2.0 >> atol) matches only if run_sweep
    hands config.rtol to values_match — pins the §1.1 threading end-to-end."""
    harness = _load_harness_module()
    trials, _ = _run_sweep_with_output_values(harness, tmp_path, 1.0e16, 1.0e16 + 2.0)
    assert trials and all(trial.match for trial in trials)


def test_run_sweep_fails_divergence_between_rtol_and_atol_scales(
    tmp_path: Path,
) -> None:
    """rel err 1e-9 must not match: kills a rtol=config.atol transposition at
    the values_match call site, under which 1e-9 <= 1e-6 would silently pass."""
    harness = _load_harness_module()
    trials, _ = _run_sweep_with_output_values(harness, tmp_path, 1.0e16, 1.0e16 + 1.0e7)
    assert trials and not any(trial.match for trial in trials)
