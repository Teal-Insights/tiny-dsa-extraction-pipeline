"""Unit tests for differential harness comparison and scenario gating."""

from __future__ import annotations

import csv
import dataclasses
import importlib
import math
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests.differential.differential_types import ATOL, RTOL, Scenario


def _load_harness_module():
    return importlib.import_module(
        "tests.differential.differential_test_exported_library"
    )


def _sample_config(tmp_path: Path):
    harness = _load_harness_module()
    workbook = tmp_path / "workbook.xlsx"
    workbook.write_bytes(b"fake-workbook")
    return harness.DifferentialConfig(
        workbook_path=workbook,
        package_dir=tmp_path / "package",
        package_name="example.api",
        import_root=tmp_path,
        report_dir=tmp_path / "reports",
        library_name="Example",
    )


def _txt_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "config": _sample_config(tmp_path),
        "environment": {
            "python": "3.13.0",
            "os": "test-os",
            "excel_grapher": "0.0.0",
        },
        "workbook_sha256": "abc123",
    }


def _sample_comparisons(harness) -> list:
    return [
        harness.compare_cell(
            "scenario:a",
            "Outputs!B1",
            "result[year=1]",
            1.0,
            1.0,
            atol=ATOL,
            rtol=RTOL,
        ),
        harness.compare_cell(
            "scenario:a",
            "Outputs!B2",
            "result[year=2]",
            2.0,
            2.01,
            atol=ATOL,
            rtol=RTOL,
        ),
    ]


@pytest.mark.parametrize(
    ("excel", "mvp", "expected_pass"),
    [
        (None, None, True),
        (None, 1.0, False),
        (1.0, None, False),
        (float("nan"), float("nan"), True),
        (float("inf"), float("inf"), True),
        (float("-inf"), float("-inf"), True),
        (1.0000001, 1.0, True),
        (1.01, 1.0, False),
    ],
)
def test_compare_cell_ladder(
    excel: Any,
    mvp: Any,
    expected_pass: bool,
) -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        excel,
        mvp,
        atol=ATOL,
        rtol=RTOL,
    )
    assert comparison.passed is expected_pass


def test_compare_cell_passes_within_atol() -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        1.0000001,
        1.0,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is True


def test_compare_cell_fails_outside_atol() -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        1.01,
        1.0,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is False


def test_compare_cell_passes_machine_noise_at_extreme_magnitude() -> None:
    """§1.1 hybrid gate: 1-ULP disagreement at 1e16 passes via the rtol branch."""
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        1.0e16,
        1.0e16 + 2.0,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is True
    assert comparison.abs_diff == pytest.approx(2.0)


def test_compare_cell_fails_genuine_divergence_under_hybrid_gate() -> None:
    """A large relative divergence must keep failing under the hybrid gate."""
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        51.137230546619406,
        1075.1372305466193,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is False


def test_compare_cell_anchors_rel_diff_to_the_golden_value() -> None:
    """rel_diff is abs_diff/|golden| exactly; an argument swap would record
    1000/(1e15+1000) here instead and this assertion would fail."""
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        1.0e15,
        1.0e15 + 1000.0,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is True
    assert comparison.rel_diff == 1e-12


def test_compare_cell_zero_golden_never_passes_relative_branch() -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        0.0,
        1e-5,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is False
    assert comparison.rel_diff == math.inf


def test_compare_cell_negative_golden_uses_magnitude() -> None:
    harness = _load_harness_module()
    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        -1.0e16,
        -(1.0e16 + 2.0),
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is True


def test_harness_exports_rtol_constant_and_config_default(tmp_path: Path) -> None:
    harness = _load_harness_module()
    assert harness.RTOL == 1e-12
    config = _sample_config(tmp_path)
    assert config.atol == 1e-6
    assert config.rtol == 1e-12


def test_txt_summary_header_reports_both_tolerances(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = [
        harness.compare_cell(
            "scenario:a",
            "Outputs!B1",
            "result[year=1]",
            1.0,
            1.0,
            atol=1e-6,
            rtol=1e-12,
        )
    ]
    report_path = tmp_path / "parity_report.txt"
    harness.write_txt_summary(comparisons, report_path, **_txt_kwargs(tmp_path))
    text = report_path.read_text(encoding="utf-8")
    assert "Tolerance: atol = 1e-06, rtol = 1e-12" in text
    assert "Workbook SHA-256: abc123" in text
    assert "ENVIRONMENT" in text
    assert "Python:  3.13.0" in text


def test_compare_cell_matches_excel_error_string_to_xlerror() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#VALUE!",
        XlError.VALUE,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is True


def test_compare_cell_rejects_different_error_classes() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#DIV/0!",
        XlError.NA,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is False
    assert comparison.matched_error is False


def test_compare_cell_flags_matched_errors_for_review() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#N/A",
        XlError.NA,
        atol=1e-6,
        rtol=1e-12,
    )
    assert comparison.passed is True
    assert comparison.matched_error is True
    assert comparison.flagged_matched_error is True


def test_compare_cell_suppresses_flag_when_scenario_expects_errors() -> None:
    harness = _load_harness_module()
    from excel_grapher import XlError

    comparison = harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#N/A",
        XlError.NA,
        atol=1e-6,
        rtol=1e-12,
        expects_error_values=True,
    )
    assert comparison.matched_error is True
    assert comparison.flagged_matched_error is False


def _matched_error_comparison(harness):
    return harness.compare_cell(
        "scenario:a",
        "Outputs!B1",
        "result[year=1]",
        "#N/A",
        "#N/A",
        atol=ATOL,
        rtol=RTOL,
    )


def test_write_txt_summary_fails_on_flagged_matched_errors(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = [_matched_error_comparison(harness)]
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, **_txt_kwargs(tmp_path))

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            FAIL" in text
    assert "MATCHED ERROR VALUES" in text


def test_write_txt_summary_passes_when_matched_errors_allowed(tmp_path: Path) -> None:
    harness = _load_harness_module()
    config = dataclasses.replace(_sample_config(tmp_path), allow_matched_errors=True)
    comparisons = [_matched_error_comparison(harness)]
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(
        comparisons,
        report_path,
        config=config,
        environment={
            "python": "3.13.0",
            "os": "test-os",
            "excel_grapher": "0.0.0",
        },
        workbook_sha256="abc123",
    )

    text = report_path.read_text(encoding="utf-8")
    assert "Result:            PASS" in text
    assert "MATCHED ERROR VALUES" in text
    assert "allowed by --allow-matched-errors" in text


def test_parse_args_accepts_allow_matched_errors() -> None:
    harness = _load_harness_module()
    args = harness.parse_args(["--allow-matched-errors"])
    assert args.allow_matched_errors is True
    assert harness.parse_args([]).allow_matched_errors is False


def test_parse_args_rejects_removed_warn_flag() -> None:
    harness = _load_harness_module()
    with pytest.raises(SystemExit):
        harness.parse_args(["--warn-on-error-values"])


def test_crash_comparisons_attribute_mvp_crash_and_keep_graph_values() -> None:
    harness = _load_harness_module()
    scenario = Scenario(id="scenario:crash", inputs={})
    cell_labels = (
        ("result[year=1]", "Outputs!B1"),
        ("result[year=2]", "Outputs!B2"),
    )
    graph_outputs = {"Outputs!B1": 1.0, "Outputs!B2": 2.0}
    exc = RuntimeError("oracle crashed")

    comparisons = harness.crash_comparisons(
        scenario,
        cell_labels,
        exc,
        crashed_oracle="mvp",
        graph_outputs=graph_outputs,
    )

    assert len(comparisons) == len(cell_labels)
    err_repr = f"<exception: {type(exc).__name__}: {exc}>"
    for comparison, (cell_label, cell_address) in zip(
        comparisons, cell_labels, strict=True
    ):
        assert comparison.scenario_id == scenario.id
        assert comparison.cell_address == cell_address
        assert comparison.cell_label == cell_label
        assert comparison.graph_value == graph_outputs[cell_address]
        assert comparison.mvp_value == err_repr
        assert comparison.abs_diff is None
        assert comparison.rel_diff is None
        assert comparison.passed is False
        assert comparison.note == "mvp oracle crashed"


def test_crash_comparisons_attribute_graph_crash() -> None:
    harness = _load_harness_module()
    scenario = Scenario(id="scenario:crash", inputs={})
    cell_labels = (("result[year=1]", "Outputs!B1"),)
    exc = RuntimeError("graph down")

    comparisons = harness.crash_comparisons(
        scenario, cell_labels, exc, crashed_oracle="graph"
    )

    assert len(comparisons) == 1
    comparison = comparisons[0]
    err_repr = f"<exception: {type(exc).__name__}: {exc}>"
    assert comparison.graph_value == err_repr
    assert comparison.mvp_value is None
    assert comparison.abs_diff is None
    assert comparison.rel_diff is None
    assert comparison.passed is False
    assert comparison.note == "graph oracle crashed"


def test_write_csv_report_round_trip(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = _sample_comparisons(harness)
    report_path = tmp_path / "parity_report.csv"

    harness.write_csv_report(comparisons, report_path)

    with report_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(harness.CSV_COLUMNS)
    assert len(rows) == len(comparisons) + 1


def test_write_txt_summary_pass(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = [
        harness.compare_cell(
            "scenario:a",
            "Outputs!B1",
            "result[year=1]",
            1.0,
            1.0,
            atol=ATOL,
            rtol=RTOL,
        )
    ]
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, **_txt_kwargs(tmp_path))

    text = report_path.read_text(encoding="utf-8")
    assert "Acceptance bar:    100.00%" in text
    assert "Result:            PASS" in text
    assert "First divergence:" not in text
    assert "FAILING COMPARISONS" not in text


def test_write_txt_summary_fail_includes_first_divergence(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = _sample_comparisons(harness)
    report_path = tmp_path / "parity_report.txt"

    harness.write_txt_summary(comparisons, report_path, **_txt_kwargs(tmp_path))

    text = report_path.read_text(encoding="utf-8")
    assert "Acceptance bar:    100.00%" in text
    assert "Result:            FAIL" in text
    assert "First divergence:" in text
    assert "Outputs!B2" in text
    assert "result[year=2]" in text
    assert "FAILING COMPARISONS (1):" in text


def test_txt_summary_lists_every_failing_comparison(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = [
        harness.compare_cell("s", "A1", "a", 1.0, 2.0, atol=ATOL, rtol=RTOL),
        harness.compare_cell("s", "A2", "b", 3.0, 4.0, atol=ATOL, rtol=RTOL),
    ]
    report_path = tmp_path / "parity_report.txt"
    harness.write_txt_summary(comparisons, report_path, **_txt_kwargs(tmp_path))
    text = report_path.read_text(encoding="utf-8")
    assert "FAILING COMPARISONS (2):" in text
    assert "A1" in text and "A2" in text


def test_harness_conforms_to_standard() -> None:
    harness = _load_harness_module()

    assert harness.ATOL == 1e-6
    assert harness.RTOL == 1e-12
    assert hasattr(harness, "compare_cell")
    assert hasattr(harness, "crash_comparisons")
    assert hasattr(harness, "write_csv_report")
    assert hasattr(harness, "write_txt_summary")
    assert harness.CSV_COLUMNS == (
        "scenario_id",
        "cell_address",
        "cell_label",
        "graph_value",
        "mvp_value",
        "abs_diff",
        "rel_diff",
        "passed",
        "matched_error",
        "flagged_matched_error",
        "note",
    )
    assert "apply_inputs_to_mvp" not in harness._REQUIRED_WORKBOOK_HOOKS
    assert "XlwingsExcelOracle" not in dir(harness)

    both_blank = harness.compare_cell(
        "s", "A1", "label", None, None, atol=ATOL, rtol=RTOL
    )
    assert both_blank.passed is True

    one_blank = harness.compare_cell(
        "s", "A1", "label", None, 1.0, atol=ATOL, rtol=RTOL
    )
    assert one_blank.passed is False

    nan_vs_number = harness.compare_cell(
        "s",
        "A1",
        "label",
        float("nan"),
        1.0,
        atol=ATOL,
        rtol=RTOL,
    )
    assert nan_vs_number.passed is False


def test_txt_summary_names_formula_evaluator_as_oracle(tmp_path: Path) -> None:
    harness = _load_harness_module()
    comparisons = [
        harness.compare_cell(
            "scenario:a",
            "Outputs!B1",
            "result[year=1]",
            1.0,
            1.0,
            atol=ATOL,
            rtol=RTOL,
        )
    ]
    report_path = tmp_path / "parity_report.txt"
    harness.write_txt_summary(comparisons, report_path, **_txt_kwargs(tmp_path))
    text = report_path.read_text(encoding="utf-8")
    assert "FormulaEvaluator" in text
    assert "vs Excel" not in text


class _BoundSeries:
    """Stand-in for ``data.*_DEFAULT`` named-axis series."""

    def __init__(self, values: tuple[object, ...]) -> None:
        self.values = values

    def with_values(self, values: tuple[object, ...]) -> _BoundSeries:
        return _BoundSeries(tuple(values))


class _YearTensor:
    """Stand-in for a ``compute_*`` named-axis result keyed by TIME_PERIOD."""

    def __init__(self, values: tuple[float, ...]) -> None:
        keys = tuple(range(1, len(values) + 1))
        self.domain = types.SimpleNamespace(axes=(types.SimpleNamespace(keys=keys),))
        self._by_key = dict(zip(keys, values, strict=True))

    def __getitem__(self, key: int) -> float:
        return self._by_key[key]


def test_mvp_outputs_for_scenario_uses_keyword_only_computes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverted-tree public API has no make_context / set_*; scenarios call compute_*."""
    harness = _load_harness_module()
    calls: list[str] = []

    def compute_output_baseline(**kwargs: object) -> _YearTensor:
        calls.append("baseline")
        assert "ctx" not in kwargs
        growth = kwargs["growth_baseline"]
        assert isinstance(growth, _BoundSeries)
        assert growth.values == (3.5, 3.5, 3.5, 3.5, 3.5)
        return _YearTensor((1.0, 2.0, 3.0, 4.0, 5.0))

    def compute_output_shocked(**kwargs: object) -> _YearTensor:
        calls.append("shocked")
        magnitudes = kwargs["shock_magnitudes"]
        assert isinstance(magnitudes, _BoundSeries)
        assert magnitudes.values == (-2.0, 0.0, 0.0)
        return _YearTensor((1.5, 2.5, 3.5, 4.5, 5.5))

    def compute_output_delta(**kwargs: object) -> _YearTensor:
        calls.append("delta")
        return _YearTensor((0.5, 0.5, 0.5, 0.5, 0.5))

    data_mod: Any = types.ModuleType("tiny_dsa_fake.data")
    data_mod.COUNTRY_INITIAL_DEBT_DEFAULT = _BoundSeries((60.0, 80.0, 40.0))
    data_mod.GROWTH_BASELINE_DEFAULT = _BoundSeries((3.5, 3.5, 3.5, 3.5, 3.5))
    data_mod.INTEREST_BASELINE_DEFAULT = _BoundSeries((4.0, 4.0, 4.0, 4.0, 4.0))
    data_mod.PRIMARY_BALANCE_BASELINE_DEFAULT = _BoundSeries(
        (-1.0, -0.5, 0.0, 0.5, 1.0)
    )
    data_mod.SHOCK_MAGNITUDES_DEFAULT = _BoundSeries((0.0, 0.0, 0.0))
    pkg: Any = types.ModuleType("tiny_dsa_fake")
    pkg.data = data_mod
    api: Any = types.ModuleType("tiny_dsa_fake.api")
    api.__package__ = "tiny_dsa_fake"
    api.compute_output_baseline = compute_output_baseline
    api.compute_output_shocked = compute_output_shocked
    api.compute_output_delta = compute_output_delta
    monkeypatch.setitem(sys.modules, "tiny_dsa_fake", pkg)
    monkeypatch.setitem(sys.modules, "tiny_dsa_fake.data", data_mod)

    scenario = harness._scenario(
        "canonical:Borvelia:growth_shock",
        dataclasses.replace(
            harness.CANONICAL_BASELINE,
            shock_type=1,
            shock_table=(-2.0, 0.0, 0.0),
        ),
    )
    outputs = harness.mvp_outputs_for_scenario(api, scenario)
    assert calls == ["baseline", "shocked", "delta"]
    assert outputs["output_baseline[year=1]"] == 1.0
    assert outputs["output_shocked[year=1]"] == 1.5
    assert outputs["output_delta[year=5]"] == 0.5
    assert not hasattr(api, "make_context")


def test_run_differential_test_requires_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate stays empty-hooks even when derived repos author a real matrix."""
    harness = _load_harness_module()
    monkeypatch.setattr(harness, "build_scenarios", lambda: ())
    monkeypatch.setattr(harness, "output_cell_labels", lambda: ())
    config = _sample_config(tmp_path)
    monkeypatch.setattr(harness, "_verify_paths", lambda config: None)
    monkeypatch.setattr(harness, "_check_staleness", lambda config: None)
    with pytest.raises(RuntimeError, match="No differential scenarios"):
        harness.run_differential_test(config)


def _stub_cell_labels():
    return (("out[1]", "Outputs!B1"), ("out[2]", "Outputs!B2"))


def _run_with_stub_oracles(tmp_path, graph_oracle, mvp_oracle, monkeypatch):
    harness = _load_harness_module()
    scenario = Scenario(id="stub:one", inputs={})
    monkeypatch.setattr(harness, "build_scenarios", lambda: (scenario,))
    monkeypatch.setattr(harness, "output_cell_labels", _stub_cell_labels)
    monkeypatch.setattr(harness, "inputs_for_excel", lambda s: {})
    monkeypatch.setattr(harness, "mvp_outputs_for_scenario", lambda api, scenario: {})
    monkeypatch.setattr(harness, "expressible_input_cells", lambda: None)
    monkeypatch.setattr(harness, "_verify_paths", lambda config: None)
    monkeypatch.setattr(harness, "_check_staleness", lambda config: None)
    monkeypatch.setattr(harness, "load_exported_library", lambda root, name: object())
    config = dataclasses.replace(
        _sample_config(tmp_path), report_dir=tmp_path / "reports"
    )
    return harness.run_differential_test(
        config,
        graph_oracle=graph_oracle,
        mvp_oracle=mvp_oracle,
    )


def test_runner_passes_relative_branch_noise_via_config(tmp_path, monkeypatch) -> None:
    """End-to-end §1.1 threading: 1-ULP noise at 1e16 (abs_diff=2.0 >> atol)
    reaches exit 0 only if run_differential_test hands config.rtol to the gate."""
    exit_code = _run_with_stub_oracles(
        tmp_path,
        graph_oracle=lambda scenario, addrs: {
            "Outputs!B1": 1.0e16,
            "Outputs!B2": 2.0,
        },
        mvp_oracle=lambda s: {"out[1]": 1.0e16 + 2.0, "out[2]": 2.0},
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0


def test_runner_fails_divergence_between_rtol_and_atol_scales(
    tmp_path, monkeypatch
) -> None:
    """rel err 1e-9 must fail end-to-end: kills a rtol=config.atol transposition,
    under which 1e-9 <= 1e-6 would silently pass."""
    exit_code = _run_with_stub_oracles(
        tmp_path,
        graph_oracle=lambda scenario, addrs: {
            "Outputs!B1": 1.0e16,
            "Outputs!B2": 2.0,
        },
        mvp_oracle=lambda s: {"out[1]": 1.0e16 + 1.0e7, "out[2]": 2.0},
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1


def test_runner_attributes_mvp_crash(tmp_path, monkeypatch) -> None:
    def boom(scenario: Scenario) -> dict[str, Any]:
        raise RuntimeError("mvp boom")

    exit_code = _run_with_stub_oracles(
        tmp_path,
        graph_oracle=lambda scenario, addrs: {
            "Outputs!B1": 1.0,
            "Outputs!B2": 2.0,
        },
        mvp_oracle=boom,
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1
    csv_text = (tmp_path / "reports" / "parity_report.csv").read_text(encoding="utf-8")
    assert "mvp oracle crashed" in csv_text
    assert "1.0" in csv_text
    assert "RuntimeError: mvp boom" in csv_text


def test_matching_stub_oracles_yield_exit_0(tmp_path, monkeypatch) -> None:
    exit_code = _run_with_stub_oracles(
        tmp_path,
        graph_oracle=lambda scenario, addrs: {
            "Outputs!B1": 1.0,
            "Outputs!B2": 2.0,
        },
        mvp_oracle=lambda s: {"out[1]": 1.0, "out[2]": 2.0},
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0


def test_comparison_stage_crash_is_contained_per_scenario(
    tmp_path, monkeypatch
) -> None:
    """§1.5: an exception raised while comparing must be recorded as failing
    comparisons and still produce reports, not abort the run."""
    harness = _load_harness_module()

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("comparison exploded")

    monkeypatch.setattr(harness, "compare_scenario", _boom)
    exit_code = _run_with_stub_oracles(
        tmp_path,
        graph_oracle=lambda scenario, addrs: {
            "Outputs!B1": 1.0,
            "Outputs!B2": 2.0,
        },
        mvp_oracle=lambda s: {"out[1]": 1.0, "out[2]": 2.0},
        monkeypatch=monkeypatch,
    )
    assert exit_code == 1
    text = (tmp_path / "reports" / "parity_report.txt").read_text(encoding="utf-8")
    assert "Result:            FAIL" in text
    csv_text = (tmp_path / "reports" / "parity_report.csv").read_text(encoding="utf-8")
    assert "comparison oracle crashed" in csv_text
    assert "1.0" in csv_text
    assert "2.0" in csv_text


def test_preflight_rejects_non_expressible_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness_module()
    scenario = Scenario(id="rogue", inputs={})
    monkeypatch.setattr(harness, "build_scenarios", lambda: (scenario,))
    monkeypatch.setattr(harness, "output_cell_labels", _stub_cell_labels)
    monkeypatch.setattr(
        harness, "inputs_for_excel", lambda s: {"Inputs!A1": 1.0, "Hidden!Z9": 0}
    )
    monkeypatch.setattr(harness, "mvp_outputs_for_scenario", lambda *a, **k: {})
    monkeypatch.setattr(
        harness, "expressible_input_cells", lambda: frozenset({"Inputs!A1"})
    )
    monkeypatch.setattr(harness, "_verify_paths", lambda config: None)
    monkeypatch.setattr(harness, "_check_staleness", lambda config: None)
    monkeypatch.setattr(harness, "load_exported_library", lambda root, name: object())

    with pytest.raises(RuntimeError, match="Input-symmetry pre-flight"):
        harness.run_differential_test(_sample_config(tmp_path))


def test_preflight_skips_when_expressible_set_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code = _run_with_stub_oracles(
        tmp_path,
        graph_oracle=lambda scenario, addrs: {
            "Outputs!B1": 1.0,
            "Outputs!B2": 2.0,
        },
        mvp_oracle=lambda s: {"out[1]": 1.0, "out[2]": 2.0},
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0


def test_staleness_hard_fails_on_fixture_hash_mismatch(tmp_path: Path) -> None:
    harness = _load_harness_module()
    package_dir = tmp_path / "dist" / "pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "api.py").write_text("", encoding="utf-8")
    workbook = tmp_path / "workbook.xlsx"
    workbook.write_bytes(b"current")
    fixture_dir = tmp_path / "dist" / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "workbook.xlsx").write_bytes(b"exported")
    config = harness.DifferentialConfig(
        workbook_path=workbook,
        package_dir=package_dir,
        package_name="pkg.api",
        import_root=tmp_path / "dist",
        report_dir=tmp_path / "reports",
        library_name="Example",
    )
    with pytest.raises(RuntimeError, match="Workbook SHA-256 mismatch"):
        harness._check_staleness(config)


def test_staleness_passes_on_identical_fixture(tmp_path: Path) -> None:
    harness = _load_harness_module()
    package_dir = tmp_path / "dist" / "pkg"
    package_dir.mkdir(parents=True)
    workbook = tmp_path / "workbook.xlsx"
    workbook.write_bytes(b"same-bytes")
    fixture_dir = tmp_path / "dist" / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "workbook.xlsx").write_bytes(b"same-bytes")
    config = harness.DifferentialConfig(
        workbook_path=workbook,
        package_dir=package_dir,
        package_name="pkg.api",
        import_root=tmp_path / "dist",
        report_dir=tmp_path / "reports",
        library_name="Example",
    )
    harness._check_staleness(config)
