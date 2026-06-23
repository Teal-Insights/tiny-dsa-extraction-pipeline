"""Tiny DSA-specific Phase B simplifiers.

These AST rewrites encode Borvelia workbook control-flow shapes (CHOOSE blocks,
``eng10``/``col10`` naming, ``func_map`` cleanup). They are not portable to other
workbooks and must be registered explicitly via ``TINY_DSA_PHASE_B_PLUGINS``.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

from src.internals_refactor import (
    HelperRewriteBinding,
    _XlEvalRewriteVisitor,
    _apply_segment_replacements,
    _function_names,
    _wrapper_body_start_line,
    _xl_eval_callee_function,
)

TINY_DSA_ENGINE_COLUMN_LETTERS = frozenset("cdefg")


def apply_tiny_dsa_phase_b_plugins(source: str) -> str:
    function_names = _function_names(source)
    updated = source
    if "primary_balance_shocked" in function_names and "shock_active" in function_names:
        updated = _simplify_primary_balance_shocked(updated)
    if (
        "output_delta" in function_names
        and "debt_to_gdp" in function_names
        and "baseline_debt" in function_names
    ):
        updated = _simplify_output_delta(updated)
    if (
        "debt_to_gdp" in function_names
        and "shock_active" in function_names
        and "primary_balance_shocked" in function_names
    ):
        updated = _simplify_debt_to_gdp(updated)
    return updated


TINY_DSA_PHASE_B_PLUGINS: tuple[Callable[[str], str], ...] = (
    apply_tiny_dsa_phase_b_plugins,
)


def _simplify_primary_balance_shocked(source: str) -> str:
    module = ast.parse(source)
    segment_replacements: list[tuple[int, int, str, str]] = []
    for node in module.body:
        if (
            not isinstance(node, ast.FunctionDef)
            or node.name != "primary_balance_shocked"
        ):
            continue
        visitor = _XlEvalRewriteVisitor(
            binding=HelperRewriteBinding(
                address="Engine!C10",
                function_name="cell_engine_c10",
                engine_column="C",
                helper_name="shock_active",
            ),
            caller_has_col=True,
            source=source,
            replacements=segment_replacements,
        )
        visitor.visit(node)
        updated = (
            _apply_segment_replacements(source, segment_replacements)
            if segment_replacements
            else source
        )
        return _remove_unused_func_map(updated, "primary_balance_shocked")
    return source


def _remove_unused_func_map(source: str, function_name: str) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if "func_map" not in target.id:
                continue
            loaded_names = {
                name.id
                for name in ast.walk(node)
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
            }
            if target.id in loaded_names:
                continue
            start = statement.lineno
            end = statement.end_lineno or start
            return "".join(lines[: start - 1]) + "".join(lines[end:])
    return source


def _simplify_output_delta(source: str) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "output_delta":
            body_start = _wrapper_body_start_line(node)
            body_end = node.end_lineno or body_start
            new_body = (
                "    return xl_sub(debt_to_gdp(ctx, col), baseline_debt(ctx, col))\n"
            )
            return (
                "".join(lines[: body_start - 1]) + new_body + "".join(lines[body_end:])
            )
    return source


def _simplify_debt_to_gdp(source: str) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "debt_to_gdp":
            continue
        assign_start: int | None = None
        assign_end: int | None = None
        for statement in node.body:
            if not isinstance(statement, ast.If):
                continue
            if _if_block_is_column_member_dispatch(statement):
                assign_start = statement.lineno
                assign_end = statement.end_lineno
                break
        if assign_start is None or assign_end is None:
            return source
        replacement = (
            "    eng10 = shock_active(ctx, col)\n"
            "    eng16 = primary_balance_shocked(ctx, col)\n"
        )
        tail = "".join(lines[assign_end:])
        tail = tail.replace("col10", "eng10").replace("col16", "eng16")
        return "".join(lines[: assign_start - 1]) + replacement + tail
    return source


def _if_block_assigns_uniform_eng_values(statement: ast.If) -> bool:
    branches = _collect_if_branches(statement)
    if len(branches) < 2:
        return False
    expected = (
        ("eng10", "shock_active"),
        ("eng16", "primary_balance_shocked"),
    )
    for branch in branches[:-1]:
        assigns = _branch_eng_assignments(branch)
        if assigns != expected:
            return False
    return True


def _collect_if_branches(statement: ast.If) -> list[list[ast.stmt]]:
    branches: list[list[ast.stmt]] = [statement.body]
    current = statement
    while (
        current.orelse
        and len(current.orelse) == 1
        and isinstance(current.orelse[0], ast.If)
    ):
        current = current.orelse[0]
        branches.append(current.body)
    if current.orelse:
        branches.append(current.orelse)
    return branches


def _branch_eng_assignments(body: list[ast.stmt]) -> tuple[tuple[str, str], ...] | None:
    assignments: list[tuple[str, str]] = []
    for statement in body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id not in {"eng10", "eng16"}:
            continue
        if not (
            isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
        ):
            return None
        assignments.append((target.id, statement.value.func.id))
    ordered = tuple(
        item for name in ("eng10", "eng16") for item in assignments if item[0] == name
    )
    if len(ordered) != 2:
        return None
    return ordered


def _if_block_is_column_member_dispatch(statement: ast.If) -> bool:
    if _if_block_assigns_uniform_eng_values(statement):
        return True
    for node in ast.walk(statement):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "xl_eval":
            callee = _xl_eval_callee_function(node)
            if callee is not None and _is_column_engine_cell_wrapper(callee):
                return True
    return False


def _is_column_engine_cell_wrapper(name: str) -> bool:
    if not name.startswith("cell_engine_"):
        return False
    suffix = name[len("cell_engine_") :]
    return len(suffix) >= 2 and suffix[0] in TINY_DSA_ENGINE_COLUMN_LETTERS
