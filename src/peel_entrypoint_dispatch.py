"""Unify peel-split published series behind their base entry-point helper (#143).

When a published output-leaf series is peeled across schedule units, one unit
keeps the bare ``series_id`` (the name ``api.py`` addresses) and the rest receive
``series_id_2``/``series_id_3`` suffixes (``allocate_schedule_helper_names``). The
base helper implements only its own regime, but the api-layer ``compute_*`` loop
calls it for every ``TIME_PERIOD`` in the full output-leaf range. Out-of-regime
years then run the base body and crash (e.g. a year->column map that only spans
the base regime) or silently return wrong-regime values.

This pass rewrites each peel-split series' base helper into a dispatcher: for the
dispatch-key values owned by a sibling unit, delegate to that sibling; otherwise
fall through to the base helper's own body. api.py keeps addressing the bare
series id, and the entry point now spans the union of the peel.

The peel remains legitimate (issue #138) -- this only repairs the entry point, it
never merges the sibling bodies.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Mapping, Sequence

DispatchKeyValue = int | str


def _function_defs(module: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }


def _sole_dispatch_parameter(node: ast.FunctionDef) -> str | None:
    """Return the helper's sole non-``ctx`` parameter, or ``None`` if not that shape."""
    args = node.args
    if args.vararg or args.kwarg or args.kwonlyargs:
        return None
    names = [arg.arg for arg in args.args]
    if len(names) != 2 or names[0] != "ctx":
        return None
    return names[1]


def _calls_helper(node: ast.FunctionDef, helper: str) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == helper
        for call in ast.walk(node)
    )


def _first_executable_statement(node: ast.FunctionDef) -> ast.stmt:
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1] if len(body) > 1 else body[0]
    return body[0]


def _format_domain(values: Sequence[DispatchKeyValue]) -> str:
    ordered = sorted(values, key=lambda v: (isinstance(v, str), v))
    rendered = ", ".join(repr(v) for v in ordered)
    return "{" + rendered + "}"


def _guard_lines(
    indent: str, param: str, sibling: str, domain: Sequence[DispatchKeyValue]
) -> list[str]:
    return [
        f"{indent}if {param} in {_format_domain(domain)}:",
        f"{indent}    return {sibling}(ctx, {param}={param})",
    ]


def inject_peel_entrypoint_dispatch(
    source: str,
    *,
    scheduled_helper_by_address: Mapping[str, str],
    address_to_series_id: Mapping[str, str],
    address_time_periods: Mapping[str, DispatchKeyValue],
) -> tuple[str, list[str]]:
    """Rewrite peel-split series' base helpers to delegate sibling-owned keys.

    Args:
        source: Refactored internals module source.
        scheduled_helper_by_address: Address -> owning helper name (from the locked
            schedule; a peeled series has ``series_id`` for one unit and
            ``series_id_2`` etc. for the others).
        address_to_series_id: Address -> series id (the base helper name).
        address_time_periods: Address -> its dispatch-key value (e.g. the year).

    Returns:
        ``(updated_source, rewritten_base_helper_names)``. When nothing qualifies
        the source is returned byte-for-byte unchanged.
    """
    module = ast.parse(source)
    defs = _function_defs(module)

    # Per series: the set of owning helpers, and per helper: the dispatch-key
    # values it owns. Only addresses that carry both a scheduled owner and a known
    # dispatch key contribute (an address without a key can't be routed).
    helpers_by_series: dict[str, set[str]] = defaultdict(set)
    domain_by_helper: dict[str, set[DispatchKeyValue]] = defaultdict(set)
    for address, helper in scheduled_helper_by_address.items():
        series_id = address_to_series_id.get(address)
        key = address_time_periods.get(address)
        if series_id is None or key is None:
            continue
        helpers_by_series[series_id].add(helper)
        domain_by_helper[helper].add(key)

    # Collect edits as (insert_line_index_0based, [lines]); apply bottom-up so
    # earlier edits don't shift later insertion points.
    edits: list[tuple[int, list[str]]] = []
    rewritten: list[str] = []
    source_lines = source.splitlines()

    for series_id, helpers in sorted(helpers_by_series.items()):
        base = series_id
        siblings = sorted(h for h in helpers if h != base)
        if not siblings:
            continue  # sole-unit series -- nothing to unify.
        base_node = defs.get(base)
        if base_node is None:
            continue  # base unit didn't refactor (kept cell_* wrappers).

        base_param = _sole_dispatch_parameter(base_node)
        if base_param is None:
            continue

        # Every sibling must be a live helper with the same single dispatch param,
        # a non-empty domain, and no domain overlap with the base or each other.
        # Anything else means the peel isn't a clean key partition; skip rather
        # than emit an ambiguous or dead guard.
        base_domain = domain_by_helper.get(base, set())
        claimed = set(base_domain)
        sibling_specs: list[tuple[str, set[DispatchKeyValue]]] = []
        clean = True
        for sibling in siblings:
            sibling_node = defs.get(sibling)
            sibling_domain = domain_by_helper.get(sibling, set())
            if (
                sibling_node is None
                or _sole_dispatch_parameter(sibling_node) != base_param
                or not sibling_domain
                or claimed & sibling_domain
            ):
                clean = False
                break
            claimed |= sibling_domain
            sibling_specs.append((sibling, sibling_domain))
        if not clean:
            continue

        # Idempotency: if the base already delegates to any sibling, this pass has
        # already run (or a hand-written dispatcher exists); leave it alone.
        if any(_calls_helper(base_node, sibling) for sibling, _ in sibling_specs):
            continue

        anchor = _first_executable_statement(base_node)
        indent = " " * anchor.col_offset
        guard_lines: list[str] = []
        for sibling, sibling_domain in sibling_specs:
            guard_lines.extend(
                _guard_lines(indent, base_param, sibling, tuple(sibling_domain))
            )
        edits.append((anchor.lineno - 1, guard_lines))
        rewritten.append(base)

    if not edits:
        return source, []

    for insert_index, lines in sorted(edits, key=lambda e: e[0], reverse=True):
        source_lines[insert_index:insert_index] = lines

    trailing_newline = "\n" if source.endswith("\n") else ""
    updated = "\n".join(source_lines) + trailing_newline
    return updated, sorted(rewritten)
