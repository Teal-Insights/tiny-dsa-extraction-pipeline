"""Regression tests for empty-IF ``None`` → ``0.0`` rewrite."""

from __future__ import annotations

import ast
import importlib.metadata
import inspect
import shutil
from pathlib import Path
from textwrap import dedent

import pytest
from excel_grapher.exporter.codegen import CodeGenerator

from src.empty_if_rewrite import rewrite_empty_if_none_literals
from src.pipeline_config import load_pipeline_config
from src.refactor_parity_gate import (
    MechanicalParityUnit,
    ParityError,
    check_batched_mechanical_parity,
    exec_internals_module,
    make_eval_context,
)

RUNTIME_IMPORT = """from __future__ import annotations

from .runtime import (
    xl_cell,
    xl_compare,
)
"""

RESOLVER_SECTION = """
_RESOLVED_FORMULAS = {}

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    name = 'cell_' + address.lower().replace('!', '_').replace("'", '')
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn
"""


@pytest.fixture(scope="module")
def parity_gate_dist_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("empty_if_parity_dist")
    package_name = load_pipeline_config().dist_metadata.package_name
    package_root = root / package_name
    package_root.mkdir(parents=True)
    fixture_runtime = (
        Path(__file__).resolve().parent / "fixtures" / "parity_gate_runtime.py"
    )
    shutil.copy(fixture_runtime, package_root / "runtime.py")
    (package_root / "data.py").write_text(
        "DEFAULT_INPUTS = {}\nCONSTANTS = {}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def package_root(parity_gate_dist_root: Path) -> Path:
    from tests.fixtures.test_state import clear_runtime_caches

    clear_runtime_caches()
    return parity_gate_dist_root / load_pipeline_config().dist_metadata.package_name


def test_rewrite_empty_if_none_arms_to_zero() -> None:
    source = dedent(
        """
        def helper(ctx, indicator):
            if indicator == 'fiscal_gap_above_target':
                return (
                    0.0
                    if (_t3 := xl_bool(xl_compare('=', read_debt_target(ctx), 0.0)))
                    else (
                        None
                        if (_t2 := xl_bool(xl_compare('<=', debt(ctx), read_debt_target(ctx))))
                        else consolidation(ctx)
                    )
                )
            return None
        """
    )
    rewritten = rewrite_empty_if_none_literals(source)
    module = ast.parse(rewritten)
    helper = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    # Walk IfExp arms: the nested empty-IF arm must be 0.0, not None.
    if_exps = [n for n in ast.walk(helper) if isinstance(n, ast.IfExp)]
    assert if_exps
    none_arms = [
        arm
        for exp in if_exps
        for arm in (exp.body, exp.orelse)
        if isinstance(arm, ast.Constant) and arm.value is None
    ]
    assert none_arms == []
    zero_arms = [
        arm
        for exp in if_exps
        for arm in (exp.body, exp.orelse)
        if isinstance(arm, ast.Constant) and arm.value == 0.0
    ]
    assert zero_arms
    # Standalone ``return None`` (not an IF arm) must stay.
    assert any(
        isinstance(stmt, ast.Return)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is None
        for stmt in helper.body
    )


def test_rewrite_empty_if_is_idempotent_on_zero_arms() -> None:
    source = dedent(
        """
        def helper(ctx):
            return 0.0 if cond else other
        """
    )
    once = rewrite_empty_if_none_literals(source)
    twice = rewrite_empty_if_none_literals(once)
    assert once == twice


def test_rewrite_preserves_non_ifexp_none() -> None:
    source = dedent(
        """
        def cell(ctx):
            x = None
            return xl_index_ref(ctx, None, 1)
        """
    )
    rewritten = rewrite_empty_if_none_literals(source)
    module = ast.parse(rewritten)
    assigns = [
        n
        for n in ast.walk(module)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Constant)
        and n.value.value is None
    ]
    assert assigns
    calls = [n for n in ast.walk(module) if isinstance(n, ast.Call)]
    assert any(
        isinstance(arg, ast.Constant) and arg.value is None
        for call in calls
        for arg in call.args
    )


def _empty_if_pristine() -> str:
    """Pristine module: empty-IF cell returns ``None``; ``xl_cell`` coerces to 0."""
    return (
        RUNTIME_IMPORT
        + dedent(
            """
            def cell_baseline_x40(ctx):
                return 7.0

            def cell_baseline_x47(ctx):
                '''Formula: =IF(debt_target=0, 0, IF(debt<=target,,X40)).'''
                _t1 = 1.0  # debt target non-zero
                return (
                    (0.0)
                    if (_t3 := xl_compare('=', _t1, 0.0))
                    else (
                        (None)
                        if (_t2 := xl_compare('<=', 0.5, _t1))
                        else (xl_cell(ctx, 'Baseline!X40'))
                    )
                )
            """
        )
        + RESOLVER_SECTION
    )


def _empty_if_mechanical(*, empty_arm: float | None) -> str:
    """Mechanical helper mirroring the empty-IF member shape."""
    arm = "None" if empty_arm is None else repr(float(empty_arm))
    return (
        RUNTIME_IMPORT
        + dedent(
            f"""
            def baseline_engine_indicators_2029(ctx, indicator):
                if indicator == 'fiscal_gap_above_target':
                    _f7_t1 = 1.0
                    return (
                        0.0
                        if (_f7_t3 := xl_compare('=', _f7_t1, 0.0))
                        else (
                            {arm}
                            if (_f7_t2 := xl_compare('<=', 0.5, _f7_t1))
                            else 7.0
                        )
                    )
                raise KeyError(indicator)

            def cell_baseline_x47(ctx):
                return baseline_engine_indicators_2029(
                    ctx, indicator='fiscal_gap_above_target'
                )
            """
        )
        + RESOLVER_SECTION
    )


def _empty_if_unit() -> MechanicalParityUnit:
    return MechanicalParityUnit(
        unit_id="cluster_365_g114",
        helper_name="baseline_engine_indicators_2029",
        kind="cluster",
        member_checks=(("Baseline!X47", {"indicator": "fiscal_gap_above_target"}),),
    )


def test_empty_if_shaped_parity_fails_when_helper_returns_none(
    package_root: Path,
) -> None:
    with pytest.raises(ParityError, match="cluster_365_g114"):
        check_batched_mechanical_parity(
            package_root=package_root,
            pristine_source=_empty_if_pristine(),
            mechanical_source=_empty_if_mechanical(empty_arm=None),
            units=[_empty_if_unit()],
            input_vectors=[{}],
        )


def test_empty_if_shaped_parity_passes_after_rewrite(package_root: Path) -> None:
    mechanical = rewrite_empty_if_none_literals(_empty_if_mechanical(empty_arm=None))
    check_batched_mechanical_parity(
        package_root=package_root,
        pristine_source=_empty_if_pristine(),
        mechanical_source=mechanical,
        units=[_empty_if_unit()],
        input_vectors=[{}],
    )
    ns = exec_internals_module(mechanical, package_root=package_root)
    ctx = make_eval_context(ns, {}, package_root=package_root)
    assert (
        ns["baseline_engine_indicators_2029"](ctx, indicator="fiscal_gap_above_target")
        == 0.0
    )


def test_excel_grapher_emits_zero_for_empty_if_branch() -> None:
    """Pin upstream 3.15.3 empty-IF lowering (excel-grapher #432 / #433)."""
    version = importlib.metadata.version("excel-grapher")
    assert tuple(int(part) for part in version.split(".")[:3]) >= (3, 15, 3)

    emit_if_src = inspect.getsource(CodeGenerator._emit_if)
    assert "EmptyArgNode" in emit_if_src
    # Empty branches must lower to numeric 0, not generic EmptyArg → None.
    assert 'true_expr = (\n                "0"' in emit_if_src or '"0"' in emit_if_src
    assert "if isinstance(node.args[1], EmptyArgNode)" in emit_if_src


def test_synthesize_singleton_body_rewrites_empty_if_none_arms() -> None:
    """Mechanical singleton synthesis must not leave empty-IF ``None`` arms."""
    from src.mechanical_body import synthesize_singleton_body

    source = dedent(
        """
        def cell_baseline_x47(ctx):
            return (
                0.0
                if xl_compare('=', xl_cell(ctx, 'Baseline!X36'), 0.0)
                else (
                    None
                    if xl_compare('<=', xl_cell(ctx, 'Macrofiscal!AE19'), xl_cell(ctx, 'Baseline!X36'))
                    else xl_cell(ctx, 'Baseline!X40')
                )
            )
        """
    )
    draft = synthesize_singleton_body(source, inline_replacements={})
    module = ast.parse(
        "def _draft(ctx):\n"
        + "\n".join(f"    {line}" for line in draft.body.splitlines())
    )
    none_arms = [
        arm
        for node in ast.walk(module)
        if isinstance(node, ast.IfExp)
        for arm in (node.body, node.orelse)
        if isinstance(arm, ast.Constant) and arm.value is None
    ]
    assert none_arms == [], draft.body
    assert any(
        isinstance(arm, ast.Constant) and arm.value == 0.0
        for node in ast.walk(module)
        if isinstance(node, ast.IfExp)
        for arm in (node.body, node.orelse)
    )
