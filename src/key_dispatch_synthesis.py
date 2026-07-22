"""Plan and synthesize key-dispatch helpers for multi-regime series clusters.

When one binding series interleaves multiple formula regimes keyed by a
dimension (e.g. DSPB PB / PB* / PB Gap), Pass-1 should emit a dispatcher that
branches on that dimension rather than skipping as operand-level variation.
"""

from __future__ import annotations

import keyword
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.formula_clustering import FormulaCluster
from src.refactor_bindings import BindingKeyValue, dimension_id_to_param_name

_CELL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]{1,3}\d+\b")
# Unquoted sheet names follow Excel's conservative identifier shape (no spaces /
# hyphens). Hyphenated sheets are emitted quoted; if a binary difference cannot
# be parsed, planning must reject rather than treat it as a normal sweep.
_DIFFERENCE_RE = re.compile(
    r"^="
    r"((?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)![A-Za-z]{1,3}\d+)"
    r"-"
    r"((?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)![A-Za-z]{1,3}\d+)$"
)
_BINARY_CELL_DIFF_SIGNATURE_RE = re.compile(
    r"^=(?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)!CELL-"
    r"(?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)!CELL$"
)


@dataclass(frozen=True)
class FormulaRegime:
    """One formula shape within a series, selected by dispatch-key values."""

    dispatch_key_values: Mapping[str, BindingKeyValue]
    members: tuple[str, ...]
    canonical_formula: str


@dataclass(frozen=True)
class KeyDispatchPlan:
    """How to parameterize and branch a multi-regime series helper."""

    helper_name: str
    dispatch_dimension_id: str
    sweep_dimension_ids: tuple[str, ...]
    regimes: tuple[FormulaRegime, ...]


def _formula_signature(formula: str) -> str:
    """Normalize cell tokens so sweep-axis address shifts share one skeleton."""
    return _CELL_TOKEN_RE.sub("CELL", formula.strip())


def _composition_role_signature(
    formula: str,
    *,
    member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    dispatch_dimension_id: str,
) -> tuple[object, ...] | None:
    """Return minuend/subtrahend dispatch roles for a binary-difference formula.

    ``None`` means the formula is not a parseable difference of two cell refs.
    """
    match = _DIFFERENCE_RE.fullmatch(formula.replace(" ", ""))
    if match is None:
        return None
    minuend_addr, subtrahend_addr = match.group(1), match.group(2)
    minuend_keys = member_keys.get(minuend_addr)
    subtrahend_keys = member_keys.get(subtrahend_addr)
    if minuend_keys is None or subtrahend_keys is None:
        return ("diff", minuend_addr, subtrahend_addr)
    if (
        dispatch_dimension_id not in minuend_keys
        or dispatch_dimension_id not in subtrahend_keys
    ):
        return ("diff", minuend_addr, subtrahend_addr)
    return (
        "diff",
        minuend_keys[dispatch_dimension_id],
        subtrahend_keys[dispatch_dimension_id],
    )


def _regime_signature(
    formula: str,
    *,
    member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    dispatch_dimension_id: str,
) -> object:
    """Signature for regime homogeneity, including composition operand roles."""
    skeleton = _formula_signature(formula)
    roles = _composition_role_signature(
        formula,
        member_keys=member_keys,
        dispatch_dimension_id=dispatch_dimension_id,
    )
    if roles is not None:
        return roles
    looks_like_diff = _BINARY_CELL_DIFF_SIGNATURE_RE.fullmatch(
        skeleton
    ) is not None or ("!CELL-" in skeleton and skeleton.count("!CELL") >= 2)
    if looks_like_diff:
        # Looks like A-B of two cells but could not be parsed (e.g. hyphenated
        # unquoted sheet). Refuse to treat members as one homogeneous regime.
        return ("unparseable_diff", skeleton, formula.strip())
    return skeleton


def _regime_structure_key(signature: object) -> object:
    """Coarse key used to decide whether partitions are *structurally* distinct.

    Composition role values (which counterpart areas appear) must not by
    themselves create a multi-regime plan — that false-positives Contract-B
    skips like trade-balance into key-dispatch.
    """
    if isinstance(signature, tuple) and signature:
        if signature[0] == "diff":
            return ("diff",)
        if signature[0] == "unparseable_diff":
            return signature
    return signature


def _regime_callee_key(
    dispatch_key_values: Mapping[str, BindingKeyValue],
) -> tuple[tuple[str, BindingKeyValue], ...]:
    return tuple(sorted(dispatch_key_values.items()))


def _candidate_dispatch_dimensions(
    member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    members: Sequence[str],
    dispatch_dimension_candidates: Sequence[str] | None,
) -> tuple[str, ...]:
    if dispatch_dimension_candidates is not None:
        return tuple(dispatch_dimension_candidates)
    value_sets: dict[str, set[BindingKeyValue]] = defaultdict(set)
    for address in members:
        keys = member_keys.get(address)
        if keys is None:
            continue
        for dimension_id, value in keys.items():
            value_sets[dimension_id].add(value)
    return tuple(
        sorted(
            dimension_id
            for dimension_id, values in value_sets.items()
            if len(values) > 1
        )
    )


def _plan_for_dispatch_dimension(
    *,
    members: Sequence[str],
    formula_nodes: Mapping[str, str],
    member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    helper_name: str,
    dispatch_dimension_id: str,
) -> KeyDispatchPlan | None:
    partitions: dict[BindingKeyValue, list[str]] = defaultdict(list)
    for address in members:
        keys = member_keys.get(address)
        if keys is None or dispatch_dimension_id not in keys:
            return None
        partitions[keys[dispatch_dimension_id]].append(address)

    if len(partitions) < 2:
        return None

    regime_signatures: list[object] = []
    regimes: list[FormulaRegime] = []
    for dispatch_value in sorted(
        partitions, key=lambda value: (str(type(value)), str(value))
    ):
        group = tuple(sorted(partitions[dispatch_value]))
        signatures = {
            _regime_signature(
                formula_nodes[address],
                member_keys=member_keys,
                dispatch_dimension_id=dispatch_dimension_id,
            )
            for address in group
        }
        if len(signatures) != 1:
            # Heterogeneous formulas / composition roles under one dispatch key.
            return None
        signature = next(iter(signatures))
        if (
            isinstance(signature, tuple)
            and signature
            and signature[0] == "unparseable_diff"
        ):
            return None
        regime_signatures.append(signature)
        canonical_formula = formula_nodes[group[0]]
        regimes.append(
            FormulaRegime(
                dispatch_key_values={dispatch_dimension_id: dispatch_value},
                members=group,
                canonical_formula=canonical_formula,
            )
        )

    if len({_regime_structure_key(signature) for signature in regime_signatures}) < 2:
        # Every dispatch bucket shares one structure → ordinary sweep / not key-dispatch.
        return None

    sweep_ids: set[str] = set()
    for address in members:
        for dimension_id in member_keys[address]:
            if dimension_id != dispatch_dimension_id:
                sweep_ids.add(dimension_id)

    return KeyDispatchPlan(
        helper_name=helper_name,
        dispatch_dimension_id=dispatch_dimension_id,
        sweep_dimension_ids=tuple(sorted(sweep_ids)),
        regimes=tuple(regimes),
    )


def plan_key_dispatch(
    cluster: FormulaCluster,
    formula_nodes: Mapping[str, str],
    member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    *,
    helper_name: str,
    dispatch_dimension_candidates: Sequence[str] | None = None,
) -> KeyDispatchPlan | None:
    """Return a key-dispatch plan when members form multiple formula regimes.

    A plan is appropriate when members partition by a dispatch dimension into
    groups that each share a formula regime, while other dimensions sweep inside
    each regime. Uniform single-skeleton clusters return ``None``.
    """
    members = tuple(cluster.members)
    if not members:
        return None
    for address in members:
        if address not in formula_nodes or address not in member_keys:
            return None

    for dimension_id in _candidate_dispatch_dimensions(
        member_keys,
        members,
        dispatch_dimension_candidates,
    ):
        plan = _plan_for_dispatch_dimension(
            members=members,
            formula_nodes=formula_nodes,
            member_keys=member_keys,
            helper_name=helper_name,
            dispatch_dimension_id=dimension_id,
        )
        if plan is not None:
            return plan
    return None


def regime_callee_key(
    dispatch_key_values: Mapping[str, BindingKeyValue],
) -> tuple[tuple[str, BindingKeyValue], ...]:
    """Public wrapper for the sorted regime key used in callee/body maps."""
    return _regime_callee_key(dispatch_key_values)


def is_difference_composition_formula(formula: str) -> bool:
    """Return whether ``formula`` is a binary difference of two cell refs."""
    return _DIFFERENCE_RE.fullmatch(formula.replace(" ", "")) is not None


def _sweep_call_kwargs(plan: KeyDispatchPlan) -> str:
    return ", ".join(
        f"{dimension_id_to_param_name(dimension_id)}="
        f"{dimension_id_to_param_name(dimension_id)}"
        for dimension_id in plan.sweep_dimension_ids
    )


def _call_expr(
    callee: str,
    plan: KeyDispatchPlan,
    *,
    include_ctx: bool,
) -> str:
    kwargs = _sweep_call_kwargs(plan)
    if include_ctx:
        if kwargs:
            return f"{callee}(ctx, {kwargs})"
        return f"{callee}(ctx)"
    if kwargs:
        return f"{callee}({kwargs})"
    return f"{callee}()"


def _self_dispatch_call(
    plan: KeyDispatchPlan,
    *,
    dispatch_value: BindingKeyValue,
    include_ctx: bool,
) -> str:
    dispatch_param = dimension_id_to_param_name(plan.dispatch_dimension_id)
    kwargs = _sweep_call_kwargs(plan)
    dispatch_kwarg = f"{dispatch_param}={dispatch_value!r}"
    parts: list[str] = []
    if include_ctx:
        parts.append("ctx")
    if kwargs:
        parts.append(kwargs)
    parts.append(dispatch_kwarg)
    return f"{plan.helper_name}({', '.join(parts)})"


def _regime_by_address(plan: KeyDispatchPlan) -> dict[str, FormulaRegime]:
    by_address: dict[str, FormulaRegime] = {}
    for regime in plan.regimes:
        for address in regime.members:
            by_address[address] = regime
    return by_address


def _regime_local_name(dispatch_value: BindingKeyValue) -> str:
    """Stable local name for a composed regime operand (e.g. ``pb_star``)."""
    text = str(dispatch_value).strip().lower()
    text = text.replace("*", "_star").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    if not text:
        text = "regime"
    if text[0].isdigit() or keyword.iskeyword(text) or not text.isidentifier():
        text = f"regime_{text}"
    return text


def _indent_body_block(body: str, indent: str) -> list[str]:
    """Indent a mechanical body (no leading indent) under an ``if`` branch."""
    lines: list[str] = []
    for raw_line in body.splitlines():
        if not raw_line.strip():
            lines.append("")
            continue
        lines.append(f"{indent}{raw_line}")
    return lines


def _difference_branch_lines(
    plan: KeyDispatchPlan,
    regime: FormulaRegime,
    *,
    regime_callees: Mapping[tuple[tuple[str, BindingKeyValue], ...], str],
    indent: str,
    include_ctx: bool,
) -> list[str]:
    formula = regime.canonical_formula.replace(" ", "")
    match = _DIFFERENCE_RE.fullmatch(formula)
    if match is None:
        raise ValueError(
            f"Composition regime {regime.dispatch_key_values!r} has no "
            f"binary-difference canonical formula: {regime.canonical_formula!r}"
        )
    minuend_addr, subtrahend_addr = match.group(1), match.group(2)
    by_address = _regime_by_address(plan)

    def _operand_binding(address: str) -> tuple[str, str]:
        operand_regime = by_address.get(address)
        if operand_regime is None:
            raise ValueError(
                f"Difference operand {address!r} is not a member of the "
                f"key-dispatch plan for {plan.helper_name!r}"
            )
        dispatch_value = operand_regime.dispatch_key_values[plan.dispatch_dimension_id]
        local_name = _regime_local_name(dispatch_value)
        callee = regime_callees.get(
            _regime_callee_key(operand_regime.dispatch_key_values), ""
        )
        if callee:
            expr = _call_expr(callee, plan, include_ctx=include_ctx)
        else:
            expr = _self_dispatch_call(
                plan,
                dispatch_value=dispatch_value,
                include_ctx=include_ctx,
            )
        return local_name, expr

    minuend_name, minuend = _operand_binding(minuend_addr)
    subtrahend_name, subtrahend = _operand_binding(subtrahend_addr)
    if minuend_name == subtrahend_name:
        subtrahend_name = f"{subtrahend_name}_right"
    return [
        f"{indent}{minuend_name} = {minuend}",
        f"{indent}{subtrahend_name} = {subtrahend}",
        f"{indent}return {minuend_name} - {subtrahend_name}",
    ]


def synthesize_key_dispatch_body(
    plan: KeyDispatchPlan,
    *,
    regime_callees: Mapping[tuple[tuple[str, BindingKeyValue], ...], str],
    regime_bodies: Mapping[tuple[tuple[str, BindingKeyValue], ...], str] | None = None,
    include_ctx: bool = False,
) -> str:
    """Return a mechanical Python body that branches on the dispatch dimension.

    ``regime_callees`` maps a frozenset-like sorted tuple of
    ``(dimension_id, value)`` pairs for each regime to the callee helper name
    used in that branch. Composition regimes (e.g. PB Gap) may be represented by
    a callee of ``\"\"`` / omitted so the synthesizer emits calls to sibling
    regimes instead.

    ``regime_bodies`` optionally supplies an already-parameterized mechanical
    body (function-body indentation) to paste into a branch instead of a call.
    """
    bodies = regime_bodies or {}
    dispatch_param = dimension_id_to_param_name(plan.dispatch_dimension_id)
    lines: list[str] = []
    for regime in plan.regimes:
        key = _regime_callee_key(regime.dispatch_key_values)
        dispatch_value = regime.dispatch_key_values[plan.dispatch_dimension_id]
        lines.append(f"if {dispatch_param} == {dispatch_value!r}:")
        body = bodies.get(key)
        if body is not None:
            lines.extend(_indent_body_block(body, indent="    "))
            continue
        callee = regime_callees.get(key, "")
        if callee:
            lines.append(
                f"    return {_call_expr(callee, plan, include_ctx=include_ctx)}"
            )
            continue
        lines.extend(
            _difference_branch_lines(
                plan,
                regime,
                regime_callees=regime_callees,
                indent="    ",
                include_ctx=include_ctx,
            )
        )
    lines.append(f"raise ValueError({dispatch_param})")
    # Match mechanical drafts: no leading indent; assemblers add function indent.
    return "\n".join(lines) + "\n"
