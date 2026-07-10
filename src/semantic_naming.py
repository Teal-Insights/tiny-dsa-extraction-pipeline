"""Semantic binding record hints and refactor identifier validation."""

from __future__ import annotations

import ast
import builtins
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.refactor_bindings import BindingKeyValue

_SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

RESERVED_HELPER_NAMES: frozenset[str] = frozenset(
    {
        "_address_to_func_name",
        "_resolve_formula",
        "_RESOLVED_FORMULAS",
        "_ADDRESS_DISPATCH",
        "_SYMBOL_DISPATCH",
    }
)


@dataclass(frozen=True)
class BindingRecordHints:
    binding_keys: dict[str, BindingKeyValue] | None = None
    binding_record: dict[str, BindingKeyValue] | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.binding_keys:
            payload["binding_keys"] = dict(self.binding_keys)
        if self.binding_record:
            payload["binding_record"] = dict(self.binding_record)
        return payload

    def has_hints(self) -> bool:
        return bool(self.to_payload())


def binding_record_hints_from_cell(
    cell: Mapping[str, Any] | None,
) -> BindingRecordHints:
    if cell is None:
        return BindingRecordHints()
    key = cell.get("key")
    record = cell.get("record")
    binding_keys = dict(key) if isinstance(key, Mapping) and key else None
    binding_record = dict(record) if isinstance(record, Mapping) and record else None
    return BindingRecordHints(
        binding_keys=binding_keys,
        binding_record=binding_record,
    )


def cluster_binding_naming_hints(
    member_hints: tuple[BindingRecordHints, ...],
) -> dict[str, object]:
    cluster_payload: dict[str, object] = {}
    member_payloads = [hint.to_payload() for hint in member_hints if hint.has_hints()]
    if member_payloads:
        cluster_payload["members"] = member_payloads
    return cluster_payload


def validate_semantic_identifier(
    name: str,
    *,
    existing_names: frozenset[str],
    allow_name: str | None = None,
    reserved_names: frozenset[str] = RESERVED_HELPER_NAMES,
) -> None:
    if not name.isidentifier():
        raise ValueError(f"name is not a valid identifier: {name!r}")
    if not _SNAKE_CASE_PATTERN.fullmatch(name):
        raise ValueError(f"name must be snake_case: {name!r}")
    if name.startswith("cell_"):
        raise ValueError(f"name must not use cell_* prefix: {name!r}")
    if name in builtins.__dict__:
        raise ValueError(f"name collides with Python builtin: {name!r}")
    if name in reserved_names:
        raise ValueError(f"name collides with reserved runtime symbol: {name!r}")
    if name in existing_names and name != allow_name:
        raise ValueError(f"name {name!r} collides with existing function")


def _is_semantic_helper_def(function_def: ast.FunctionDef) -> bool:
    if function_def.name.startswith("cell_") or function_def.name.startswith("_"):
        return False
    if not function_def.args.args:
        return False
    if function_def.args.args[0].arg != "ctx":
        return False
    return True


def collect_semantic_helper_names(source: str) -> frozenset[str]:
    module = ast.parse(source)
    return frozenset(
        node.name
        for node in module.body
        if isinstance(node, ast.FunctionDef) and _is_semantic_helper_def(node)
    )


def semantic_helpers_available_for_calls(
    source: str,
    existing_names: frozenset[str],
) -> frozenset[str]:
    """Semantic helpers defined in source plus already-allocated helper names."""
    defined = collect_semantic_helper_names(source)
    allocated = frozenset(
        name
        for name in existing_names
        if name not in RESERVED_HELPER_NAMES
        and not name.startswith("cell_")
        and not name.startswith("_")
    )
    return defined | allocated
