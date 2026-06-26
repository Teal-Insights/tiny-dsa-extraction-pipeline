"""Semantic label extraction and refactor identifier validation."""

from __future__ import annotations

import ast
import builtins
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
class SemanticLabelHints:
    table_labels: str | None = None
    row_labels: str | None = None
    column_labels: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.table_labels is not None:
            payload["table_labels"] = self.table_labels
        if self.row_labels is not None:
            payload["row_labels"] = self.row_labels
        if self.column_labels is not None:
            payload["column_labels"] = self.column_labels
        return payload

    def has_labels(self) -> bool:
        return bool(self.to_payload())


def _semantic_label_text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        label = value.get("label")
        return str(label) if label is not None else None
    if isinstance(value, str | int | float):
        return str(value)
    return None


def semantic_label_group_text(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    labels = [
        label_text
        for item in value
        if (label_text := _semantic_label_text(item)) is not None
    ]
    if not labels:
        return None
    return " | ".join(labels)


def semantic_label_hints_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> SemanticLabelHints:
    if metadata is None:
        return SemanticLabelHints()
    return SemanticLabelHints(
        table_labels=semantic_label_group_text(metadata.get("table_labels")),
        row_labels=semantic_label_group_text(metadata.get("row_labels")),
        column_labels=semantic_label_group_text(metadata.get("column_labels")),
    )


def cluster_naming_hints(
    member_hints: tuple[SemanticLabelHints, ...],
) -> dict[str, object]:
    table_labels = _consensus_label(
        hint.table_labels for hint in member_hints if hint.table_labels
    )
    row_labels = _consensus_label(
        hint.row_labels for hint in member_hints if hint.row_labels
    )
    cluster_payload: dict[str, object] = {}
    if table_labels is not None:
        cluster_payload["table_labels"] = table_labels
    if row_labels is not None:
        cluster_payload["row_labels"] = row_labels
    member_payloads = [hint.to_payload() for hint in member_hints]
    if any(member_payloads):
        cluster_payload["members"] = member_payloads
    return cluster_payload


def _consensus_label(labels: Any) -> str | None:
    unique = sorted({label for label in labels if label})
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return " | ".join(unique)


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
