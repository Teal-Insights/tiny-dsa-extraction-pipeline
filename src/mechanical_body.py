"""Mechanical synthesis of parameterized cluster helper bodies (issue #45).

Consumes a cluster's fingerprint summary (structural skeletons, per-ref
relations, per-member ref addresses/keys) plus the unpacked exemplar
translations, and produces a parameterized helper body without any LLM
involvement. The synthesizer only rewrites *read sites* — ``xl_cell`` /
``xl_eval`` literals, ``read_*`` accessor arguments, semantic-helper call
arguments, and in-cluster self-recurrence — leaving every operator, coercion
wrapper, literal, and lazy branch of the exemplar untouched.

Every rewrite is verified per member: the derived address or argument keys
must equal the recorded refs for all members of the fingerprint group. Any
unsupported shape or verification mismatch raises
:class:`MechanicalSynthesisError`; callers fall back to the legacy full-body
LLM contract.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from src.refactor_bindings import BindingKeyValue, KeyConceptSpec
from src.refactor_fingerprints import (
    ClusterFingerprintSummary,
    FingerprintGroup,
    RefRelation,
)

_MECHANICAL_TEMP_PATTERN = re.compile(r"^_t\d+$")
_ACCESSOR_PREFIX = "read_"
_CELL_FUNCTION_PREFIX = "cell_"
_POINT_READ_CALLEES = frozenset({"xl_cell", "xl_eval"})
_UNSUPPORTED_READ_CALLEES = frozenset(
    {"xl_range", "xl_range_rows", "xl_index_ref", "xl_offset", "xl_match"}
)


class MechanicalSynthesisError(Exception):
    """A cluster cannot be mechanically synthesized; fall back to the LLM body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ExemplarLike(Protocol):
    address: str
    function_name: str
    python_source: str


@dataclass(frozen=True)
class MechanicalBodyDraft:
    """A verified, parameterized helper body with mechanical local names."""

    body: str
    renameable_locals: tuple[str, ...]
    lookup_table_names: tuple[str, ...]
    group_count: int


def _sort_key(value: BindingKeyValue) -> tuple[int, object]:
    if isinstance(value, bool):
        return (3, value)
    if isinstance(value, (int, float)):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return (2, str(value))


@dataclass
class _TableRegistry:
    """Allocates deterministic names for mechanical lookup-table dict literals."""

    tables: dict[str, dict[BindingKeyValue, BindingKeyValue]] = field(
        default_factory=dict
    )
    order: list[str] = field(default_factory=list)

    def register(
        self, base_name: str, content: Mapping[BindingKeyValue, BindingKeyValue]
    ) -> str:
        frozen = dict(content)
        name = base_name
        suffix = 2
        while name in self.tables:
            if self.tables[name] == frozen:
                return name
            name = f"{base_name}_{suffix}"
            suffix += 1
        self.tables[name] = frozen
        self.order.append(name)
        return name

    def assignments(self, names: Sequence[str]) -> list[ast.stmt]:
        statements: list[ast.stmt] = []
        for name in names:
            content = self.tables[name]
            keys = sorted(content, key=_sort_key)
            statements.append(
                ast.Assign(
                    targets=[ast.Name(id=name, ctx=ast.Store())],
                    value=ast.Dict(
                        keys=[ast.Constant(value=key) for key in keys],
                        values=[ast.Constant(value=content[key]) for key in keys],
                    ),
                )
            )
        return statements


@dataclass(frozen=True)
class _ReadSite:
    node: ast.Call
    callee: str
    address: str | None


def _iter_calls(node: ast.AST) -> Iterator[ast.Call]:
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _iter_calls(child)


def _call_address_literal(node: ast.Call) -> str | None:
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        if isinstance(value, str):
            return value
    return None


def _collect_read_sites(
    statements: Sequence[ast.stmt],
    semantic_helper_names: frozenset[str],
) -> list[_ReadSite]:
    sites: list[_ReadSite] = []
    for statement in statements:
        for call in _iter_calls(statement):
            func = call.func
            if not isinstance(func, ast.Name):
                continue
            callee = func.id
            if callee in _UNSUPPORTED_READ_CALLEES:
                raise MechanicalSynthesisError(f"unsupported_read_callee:{callee}")
            if callee in _POINT_READ_CALLEES:
                sites.append(
                    _ReadSite(
                        node=call, callee=callee, address=_call_address_literal(call)
                    )
                )
            elif (
                callee.startswith((_ACCESSOR_PREFIX, _CELL_FUNCTION_PREFIX))
                or callee in semantic_helper_names
            ):
                sites.append(_ReadSite(node=call, callee=callee, address=None))
    return sites


def _literal_keywords(call: ast.Call) -> dict[str, BindingKeyValue] | None:
    """Return ``{kwarg: literal}`` when every keyword is a plain literal."""
    literals: dict[str, BindingKeyValue] = {}
    for keyword in call.keywords:
        if keyword.arg is None or not isinstance(keyword.value, ast.Constant):
            return None
        value = keyword.value.value
        if isinstance(value, bytes) or not isinstance(value, (str, int, float, bool)):
            return None
        literals[keyword.arg] = value
    return literals


def _param_expr(name: str) -> ast.expr:
    return ast.Name(id=name, ctx=ast.Load())


def _offset_expr(param: str, delta: int) -> ast.expr:
    if delta < 0:
        return ast.BinOp(
            left=_param_expr(param), op=ast.Sub(), right=ast.Constant(value=-delta)
        )
    return ast.BinOp(
        left=_param_expr(param), op=ast.Add(), right=ast.Constant(value=delta)
    )


@dataclass
class _GroupSynthesizer:
    group: FingerprintGroup
    group_index: int
    multi_group: bool
    helper_name: str
    param_by_dim: dict[str, str]
    varying_dims: tuple[str, ...]
    expected_member_keys: Mapping[str, Mapping[str, BindingKeyValue]]
    tables: _TableRegistry

    def __post_init__(self) -> None:
        self.ref_addresses = dict(self.group.ref_addresses_by_member)
        self.ref_keys = dict(self.group.ref_keys_by_member)
        exemplar = self.group.exemplar
        self.exemplar_refs = self.ref_addresses.get(exemplar.address, ())
        self.exemplar_ref_keys = self.ref_keys.get(exemplar.address, ())
        self.table_names: list[str] = []
        self.replacements: dict[int, ast.expr] = {}
        self.claim_counts: dict[int, int] = {}

    # -- relation-derived key expressions and their mirror evaluation --------

    def _dim_param(self, dim: str) -> str:
        param = self.param_by_dim.get(dim)
        if param is None:
            raise MechanicalSynthesisError(f"dimension_without_parameter:{dim}")
        return param

    def _register_table(
        self, base_name: str, content: Mapping[BindingKeyValue, BindingKeyValue]
    ) -> str:
        name = self.tables.register(base_name, content)
        if name not in self.table_names:
            self.table_names.append(name)
        return name

    def _derived_key_expr(self, relation: RefRelation, dim: str) -> ast.expr:
        if dim in relation.fixed_keys:
            return ast.Constant(value=relation.fixed_keys[dim])
        if dim in relation.identity_dims:
            return _param_expr(self._dim_param(dim))
        if dim in relation.offsets:
            return _offset_expr(self._dim_param(dim), relation.offsets[dim])
        if dim in relation.lookups:
            key_dim = relation.lookup_keys.get(dim)
            if key_dim is None:
                raise MechanicalSynthesisError(f"lookup_without_key_dim:{dim}")
            key_param = self._dim_param(key_dim)
            table = relation.lookups[dim]
            if dim in relation.lookup_bases:
                table_name = self._register_table(
                    f"{self._dim_param(dim)}_lag_by_{key_param}", table
                )
                return ast.BinOp(
                    left=_param_expr(self._dim_param(dim)),
                    op=ast.Sub(),
                    right=ast.Subscript(
                        value=_param_expr(table_name),
                        slice=_param_expr(key_param),
                        ctx=ast.Load(),
                    ),
                )
            table_name = self._register_table(f"{dim.lower()}_by_{key_param}", table)
            return ast.Subscript(
                value=_param_expr(table_name),
                slice=_param_expr(key_param),
                ctx=ast.Load(),
            )
        raise MechanicalSynthesisError(f"underived_dimension:{dim}")

    def _derived_key_value(
        self,
        relation: RefRelation,
        dim: str,
        member_keys: Mapping[str, BindingKeyValue],
    ) -> BindingKeyValue:
        if dim in relation.fixed_keys:
            return relation.fixed_keys[dim]
        if dim in relation.identity_dims:
            return member_keys[dim]
        if dim in relation.offsets:
            value = member_keys[dim]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MechanicalSynthesisError(f"offset_on_non_numeric_key:{dim}")
            return value + relation.offsets[dim]
        if dim in relation.lookups:
            key_dim = relation.lookup_keys.get(dim)
            if key_dim is None or key_dim not in member_keys:
                raise MechanicalSynthesisError(f"lookup_without_key_dim:{dim}")
            table = relation.lookups[dim]
            key_value = member_keys[key_dim]
            if key_value not in table:
                raise MechanicalSynthesisError(f"lookup_key_not_covered:{dim}")
            if dim in relation.lookup_bases:
                base = member_keys[dim]
                lag = table[key_value]
                if (
                    isinstance(base, bool)
                    or isinstance(lag, bool)
                    or not isinstance(base, (int, float))
                    or not isinstance(lag, (int, float))
                ):
                    raise MechanicalSynthesisError(f"lag_on_non_numeric_key:{dim}")
                return base - lag
            return table[key_value]
        raise MechanicalSynthesisError(f"underived_dimension:{dim}")

    # -- slot matching --------------------------------------------------------

    def _keywords_match_slot(self, site: _ReadSite, slot: int) -> bool:
        literals = _literal_keywords(site.node)
        if literals is None:
            return False
        slot_keys = self.exemplar_ref_keys[slot]
        for kwarg, value in literals.items():
            dim = self._dim_for_kwarg(kwarg, slot_keys)
            if dim is None or slot_keys.get(dim) != value:
                return False
        return True

    def _dim_for_kwarg(
        self, kwarg: str, slot_keys: Mapping[str, BindingKeyValue]
    ) -> str | None:
        for dim, param in self.param_by_dim.items():
            if param == kwarg:
                return dim
        for dim in slot_keys:
            if dim.lower() == kwarg:
                return dim
        return None

    def _site_matches_slot(self, site: _ReadSite, slot: int) -> bool:
        relation = self.group.ref_relations[slot]
        if slot >= len(self.exemplar_refs):
            return False
        slot_address = self.exemplar_refs[slot]
        if site.callee in _POINT_READ_CALLEES:
            return site.address == slot_address
        if (
            relation.resolution.kind == "semantic_helper"
            and site.callee == relation.resolution.helper_name
        ):
            return self._keywords_match_slot(site, slot)
        if (
            relation.series_id is not None
            and site.callee == f"{_ACCESSOR_PREFIX}{relation.series_id}"
        ):
            return self._keywords_match_slot(site, slot)
        return False

    # -- rewrites -------------------------------------------------------------

    def _rewrite_keyword_call(self, site: _ReadSite, slot: int) -> ast.expr | None:
        relation = self.group.ref_relations[slot]
        slot_keys = self.exemplar_ref_keys[slot]
        new_keywords: list[ast.keyword] = []
        changed = False
        for keyword in site.node.keywords:
            assert keyword.arg is not None  # _keywords_match_slot enforced literals
            dim = self._dim_for_kwarg(keyword.arg, slot_keys)
            assert dim is not None
            if dim in relation.fixed_keys:
                new_keywords.append(keyword)
                continue
            new_keywords.append(
                ast.keyword(
                    arg=keyword.arg, value=self._derived_key_expr(relation, dim)
                )
            )
            changed = True
        if not changed:
            return None
        return ast.Call(func=site.node.func, args=site.node.args, keywords=new_keywords)

    def _address_template_expr(self, slot: int) -> ast.expr | None:
        """Build the parameterized address expression for an ``xl_cell`` slot."""
        relation = self.group.ref_relations[slot]
        resolution = relation.resolution
        template = resolution.address_template
        if template is None:
            raise MechanicalSynthesisError(f"missing_address_template:slot_{slot}")
        needs_col = "{col}" in template
        needs_row = "{row}" in template
        if not needs_col and not needs_row:
            self._verify_constant_slot_address(slot, template)
            return None

        parts = re.split(r"(\{col\}|\{row\})", template)
        joined: list[ast.expr] = []
        for part in parts:
            if part == "{col}":
                joined.append(
                    self._axis_lookup_expr(slot, resolution.col_by_dim, "column")
                )
            elif part == "{row}":
                joined.append(
                    self._axis_lookup_expr(slot, resolution.row_by_dim, "row")
                )
            elif part:
                joined.append(ast.Constant(value=part))
        formatted: list[ast.expr] = [
            expr
            if isinstance(expr, ast.Constant)
            else ast.FormattedValue(value=expr, conversion=-1, format_spec=None)
            for expr in joined
        ]
        return ast.JoinedStr(values=formatted)

    def _axis_lookup_expr(
        self,
        slot: int,
        axis_tables: Sequence[
            tuple[str, tuple[tuple[BindingKeyValue, BindingKeyValue], ...]]
        ],
        axis: str,
    ) -> ast.expr:
        for dim, pairs in axis_tables:
            if dim not in self.param_by_dim or dim not in self.varying_dims:
                continue
            param = self.param_by_dim[dim]
            table_name = self._register_table(f"{axis}_by_{param}", dict(pairs))
            return ast.Subscript(
                value=_param_expr(table_name),
                slice=_param_expr(param),
                ctx=ast.Load(),
            )
        raise MechanicalSynthesisError(f"no_routable_{axis}_dimension:slot_{slot}")

    def _self_recurrence_call(self, slot: int) -> ast.expr:
        relation = self.group.ref_relations[slot]
        keywords = [
            ast.keyword(
                arg=self.param_by_dim[dim], value=self._derived_key_expr(relation, dim)
            )
            for dim in self.varying_dims
        ]
        return ast.Call(
            func=ast.Name(id=self.helper_name, ctx=ast.Load()),
            args=[ast.Name(id="ctx", ctx=ast.Load())],
            keywords=keywords,
        )

    # -- verification ----------------------------------------------------------

    def _verify_constant_slot_address(self, slot: int, address: str) -> None:
        for member in self.group.members:
            recorded = self.ref_addresses[member][slot]
            if recorded != address:
                raise MechanicalSynthesisError(
                    f"constant_slot_address_mismatch:slot_{slot}:{member}"
                )

    def _verify_templated_slot_address(self, slot: int) -> None:
        relation = self.group.ref_relations[slot]
        resolution = relation.resolution
        template = resolution.address_template
        assert template is not None
        col_tables = {dim: dict(pairs) for dim, pairs in resolution.col_by_dim}
        row_tables = {dim: dict(pairs) for dim, pairs in resolution.row_by_dim}
        col_dim = self._chosen_axis_dim(resolution.col_by_dim)
        row_dim = self._chosen_axis_dim(resolution.row_by_dim)
        for member in self.group.members:
            member_keys = self.expected_member_keys.get(member, {})
            resolved = template
            if "{col}" in template:
                assert col_dim is not None
                key = member_keys.get(col_dim)
                if key not in col_tables[col_dim]:
                    raise MechanicalSynthesisError(
                        f"column_key_not_covered:slot_{slot}:{member}"
                    )
                resolved = resolved.replace("{col}", str(col_tables[col_dim][key]))
            if "{row}" in template:
                assert row_dim is not None
                key = member_keys.get(row_dim)
                if key not in row_tables[row_dim]:
                    raise MechanicalSynthesisError(
                        f"row_key_not_covered:slot_{slot}:{member}"
                    )
                resolved = resolved.replace("{row}", str(row_tables[row_dim][key]))
            if resolved != self.ref_addresses[member][slot]:
                raise MechanicalSynthesisError(
                    f"templated_address_mismatch:slot_{slot}:{member}"
                )

    def _chosen_axis_dim(
        self,
        axis_tables: Sequence[
            tuple[str, tuple[tuple[BindingKeyValue, BindingKeyValue], ...]]
        ],
    ) -> str | None:
        for dim, _pairs in axis_tables:
            if dim in self.param_by_dim and dim in self.varying_dims:
                return dim
        return None

    def _verify_derived_keys(self, slot: int) -> None:
        relation = self.group.ref_relations[slot]
        for member in self.group.members:
            member_keys = self.expected_member_keys.get(member, {})
            recorded = self.ref_keys[member][slot]
            for dim, expected_value in recorded.items():
                derived = self._derived_key_value(relation, dim, member_keys)
                if derived != expected_value:
                    raise MechanicalSynthesisError(
                        f"derived_key_mismatch:slot_{slot}:{member}:{dim}"
                    )

    def _verify_self_recurrence(self, slot: int) -> None:
        relation = self.group.ref_relations[slot]
        for member in self.group.members:
            member_keys = self.expected_member_keys.get(member, {})
            target = self.ref_addresses[member][slot]
            target_keys = self.expected_member_keys.get(target)
            if target_keys is None:
                raise MechanicalSynthesisError(
                    f"self_recurrence_target_outside_cluster:slot_{slot}:{member}"
                )
            for dim in self.varying_dims:
                derived = self._derived_key_value(relation, dim, member_keys)
                if derived != target_keys.get(dim):
                    raise MechanicalSynthesisError(
                        f"self_recurrence_key_mismatch:slot_{slot}:{member}:{dim}"
                    )

    # -- orchestration ---------------------------------------------------------

    def synthesize(self) -> tuple[list[ast.stmt], list[str]]:
        """Return the rewritten group statements and its renameable local names."""
        exemplar = self.group.exemplar
        module = ast.parse(exemplar.python_source)
        functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
        if len(functions) != 1:
            raise MechanicalSynthesisError("unsupported_exemplar_shape")
        statements = list(functions[0].body)
        if (
            statements
            and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)
        ):
            statements = statements[1:]
        if not self.exemplar_refs and self.group.ref_relations:
            raise MechanicalSynthesisError("missing_exemplar_ref_addresses")
        if len(self.exemplar_ref_keys) != len(self.group.ref_relations):
            raise MechanicalSynthesisError("missing_exemplar_ref_keys")

        semantic_helper_names = frozenset(
            relation.resolution.helper_name
            for relation in self.group.ref_relations
            if relation.resolution.kind == "semantic_helper"
            and relation.resolution.helper_name is not None
        )
        sites = _collect_read_sites(statements, semantic_helper_names)
        claimed: dict[int, int] = {}
        for site in sites:
            matching = [
                slot
                for slot in range(len(self.group.ref_relations))
                if self._site_matches_slot(site, slot)
            ]
            if not matching:
                raise MechanicalSynthesisError(
                    f"unclaimed_read_site:{ast.unparse(site.node)}"
                )
            slot = min(matching, key=lambda s: (self.claim_counts.get(s, 0), s))
            self.claim_counts[slot] = self.claim_counts.get(slot, 0) + 1
            claimed[id(site.node)] = slot
            self._rewrite_site(site, slot)

        unclaimed_slots = [
            slot
            for slot in range(len(self.group.ref_relations))
            if slot not in self.claim_counts
        ]
        if unclaimed_slots:
            raise MechanicalSynthesisError(
                f"slots_without_read_sites:{unclaimed_slots}"
            )

        for slot in range(len(self.group.ref_relations)):
            self._verify_slot(slot)

        rewritten = [
            _replace_nodes(statement, self.replacements) for statement in statements
        ]
        rewritten, locals_renamed = self._prefix_group_temporaries(rewritten)
        prelude = self.tables.assignments(self.table_names)
        return prelude + rewritten, locals_renamed

    def _rewrite_site(self, site: _ReadSite, slot: int) -> None:
        relation = self.group.ref_relations[slot]
        resolution = relation.resolution
        if site.callee == "xl_cell":
            address_expr = self._address_template_expr(slot)
            if address_expr is not None:
                self.replacements[id(site.node)] = ast.Call(
                    func=site.node.func,
                    args=[site.node.args[0], address_expr],
                    keywords=site.node.keywords,
                )
            return
        if site.callee == "xl_eval":
            if resolution.kind == "self_recurrence":
                self.replacements[id(site.node)] = self._self_recurrence_call(slot)
                return
            slot_addresses = {
                self.ref_addresses[member][slot] for member in self.group.members
            }
            if len(slot_addresses) == 1:
                return
            raise MechanicalSynthesisError(
                f"xl_eval_dependency_not_parameterizable:slot_{slot}"
            )
        if site.callee.startswith(_CELL_FUNCTION_PREFIX):
            raise MechanicalSynthesisError(
                f"direct_cell_call_unsupported:{site.callee}"
            )
        replacement = self._rewrite_keyword_call(site, slot)
        if replacement is not None:
            self.replacements[id(site.node)] = replacement

    def _verify_slot(self, slot: int) -> None:
        relation = self.group.ref_relations[slot]
        resolution = relation.resolution
        if resolution.kind == "self_recurrence":
            self._verify_self_recurrence(slot)
            return
        template = resolution.address_template
        if (
            resolution.kind == "xl_cell"
            and template is not None
            and ("{col}" in template or "{row}" in template)
        ):
            # Address templates verify against recorded addresses; keyword call
            # rewrites verify against recorded ref keys. Both hold for accessor
            # reads whose resolution predicted an xl_cell template.
            slot_addresses = {
                self.ref_addresses[member][slot] for member in self.group.members
            }
            if len(slot_addresses) > 1:
                self._verify_templated_slot_address(slot)
        self._verify_derived_keys(slot)

    def _prefix_group_temporaries(
        self, statements: list[ast.stmt]
    ) -> tuple[list[ast.stmt], list[str]]:
        renames: dict[str, str] = {}
        for statement in statements:
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and _MECHANICAL_TEMP_PATTERN.match(
                    node.id
                ):
                    if self.multi_group:
                        renames.setdefault(node.id, f"_f{self.group_index}{node.id}")
                    else:
                        renames.setdefault(node.id, node.id)
        if self.multi_group:
            renamer = _NameRenamer(renames)
            statements = [renamer.visit(statement) for statement in statements]
        return statements, sorted(set(renames.values()))


class _NameRenamer(ast.NodeTransformer):
    def __init__(self, renames: Mapping[str, str]) -> None:
        self._renames = dict(renames)

    def visit_Name(self, node: ast.Name) -> ast.Name:
        replacement = self._renames.get(node.id)
        if replacement is not None:
            return ast.Name(id=replacement, ctx=node.ctx)
        return node


class _NodeReplacer(ast.NodeTransformer):
    def __init__(self, replacements: Mapping[int, ast.expr]) -> None:
        self._replacements = dict(replacements)

    def generic_visit(self, node: ast.AST) -> ast.AST:
        replacement = self._replacements.get(id(node))
        if replacement is not None:
            return replacement
        return super().generic_visit(node)


def _replace_nodes(
    statement: ast.stmt, replacements: Mapping[int, ast.expr]
) -> ast.stmt:
    replaced = _NodeReplacer(replacements).visit(statement)
    return ast.fix_missing_locations(replaced)


def _routing_condition(
    group_members: Sequence[str],
    expected_member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    varying_dims: Sequence[str],
    param_by_dim: Mapping[str, str],
    other_groups: Sequence[Sequence[str]],
) -> ast.expr:
    """Build a membership test separating one group from all later groups."""

    def _values(members: Sequence[str], dim: str) -> set[BindingKeyValue]:
        return {expected_member_keys[member][dim] for member in members}

    for dim in varying_dims:
        own = _values(group_members, dim)
        if all(own.isdisjoint(_values(other, dim)) for other in other_groups):
            param = param_by_dim[dim]
            if len(own) == 1:
                return ast.Compare(
                    left=_param_expr(param),
                    ops=[ast.Eq()],
                    comparators=[ast.Constant(value=next(iter(own)))],
                )
            return ast.Compare(
                left=_param_expr(param),
                ops=[ast.In()],
                comparators=[
                    ast.Set(
                        elts=[
                            ast.Constant(value=value)
                            for value in sorted(own, key=_sort_key)
                        ]
                    )
                ],
            )

    params = tuple(param_by_dim[dim] for dim in varying_dims)
    own_tuples = {
        tuple(expected_member_keys[member][dim] for dim in varying_dims)
        for member in group_members
    }
    return ast.Compare(
        left=ast.Tuple(elts=[_param_expr(param) for param in params], ctx=ast.Load()),
        ops=[ast.In()],
        comparators=[
            ast.Set(
                elts=[
                    ast.Tuple(
                        elts=[ast.Constant(value=value) for value in combo],
                        ctx=ast.Load(),
                    )
                    for combo in sorted(
                        own_tuples, key=lambda t: tuple(_sort_key(v) for v in t)
                    )
                ]
            )
        ],
    )


def _verify_group_partition(
    groups: Sequence[FingerprintGroup],
    expected_member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    varying_dims: Sequence[str],
) -> None:
    seen: dict[tuple, str] = {}
    for group in groups:
        for member in group.members:
            keys = expected_member_keys.get(member)
            if keys is None:
                raise MechanicalSynthesisError(f"member_without_expected_keys:{member}")
            combo = tuple(keys.get(dim) for dim in varying_dims)
            if combo in seen and seen[combo] != member:
                raise MechanicalSynthesisError(
                    f"routing_key_collision:{member}:{seen[combo]}"
                )
            seen[combo] = member


def synthesize_cluster_body(
    summary: ClusterFingerprintSummary,
    *,
    key_vocabulary: Sequence[KeyConceptSpec],
    expected_member_keys: Mapping[str, Mapping[str, BindingKeyValue]],
    helper_name: str,
) -> MechanicalBodyDraft:
    """Synthesize a verified parameterized body for one cluster refactor unit.

    Raises :class:`MechanicalSynthesisError` when any read cannot be rewritten
    from the recorded relations or any per-member verification fails.
    """
    if summary.fallback_reason is not None:
        raise MechanicalSynthesisError(
            f"fingerprint_fallback:{summary.fallback_reason}"
        )
    if not summary.groups:
        raise MechanicalSynthesisError("no_fingerprint_groups")

    dim_sets = {frozenset(keys) for keys in expected_member_keys.values()}
    if len(dim_sets) != 1:
        raise MechanicalSynthesisError("inconsistent_varying_dimensions")
    varying_dims = tuple(sorted(next(iter(dim_sets))))
    if not varying_dims:
        raise MechanicalSynthesisError("no_varying_dimensions")
    param_by_dim = {
        spec.dimension_id: spec.suggested_param_name for spec in key_vocabulary
    }
    missing = [dim for dim in varying_dims if dim not in param_by_dim]
    if missing:
        raise MechanicalSynthesisError(f"dimensions_without_vocabulary:{missing}")

    multi_group = len(summary.groups) > 1
    if multi_group:
        _verify_group_partition(summary.groups, expected_member_keys, varying_dims)

    tables = _TableRegistry()
    group_statements: list[list[ast.stmt]] = []
    renameable: list[str] = []
    for index, group in enumerate(summary.groups, start=1):
        synthesizer = _GroupSynthesizer(
            group=group,
            group_index=index,
            multi_group=multi_group,
            helper_name=helper_name,
            param_by_dim=param_by_dim,
            varying_dims=varying_dims,
            expected_member_keys=expected_member_keys,
            tables=tables,
        )
        statements, group_locals = synthesizer.synthesize()
        group_statements.append(statements)
        renameable.extend(group_locals)

    body_statements: list[ast.stmt] = []
    if multi_group:
        for position, (group, statements) in enumerate(
            zip(summary.groups, group_statements, strict=True)
        ):
            is_last = position == len(summary.groups) - 1
            if is_last:
                body_statements.extend(statements)
            else:
                condition = _routing_condition(
                    group.members,
                    expected_member_keys,
                    varying_dims,
                    param_by_dim,
                    [g.members for g in summary.groups[position + 1 :]],
                )
                body_statements.append(
                    ast.If(test=condition, body=statements, orelse=[])
                )
    else:
        body_statements.extend(group_statements[0])

    module = ast.Module(body=body_statements, type_ignores=[])
    ast.fix_missing_locations(module)
    body = "\n".join(ast.unparse(statement) for statement in module.body)

    params = ", ".join(sorted(param_by_dim[dim] for dim in varying_dims))
    indented = "\n".join(f"    {line}" for line in body.splitlines())
    try:
        ast.parse(f"def _draft(ctx, {params}):\n{indented}\n")
    except SyntaxError as error:  # pragma: no cover - defensive
        raise MechanicalSynthesisError(f"draft_body_invalid:{error}") from error

    return MechanicalBodyDraft(
        body=body,
        renameable_locals=tuple(sorted(set(renameable))),
        lookup_table_names=tuple(tables.order),
        group_count=len(summary.groups),
    )
