"""Mechanical return-type inference for refactor helpers.

Resolves helper return annotations from mechanical member bodies using the
closed-world call graph: runtime and internals function signatures already
present in generated code, plus a small allowlisted map for ``xl_*`` helpers.
Opaque ``xl_*`` passthroughs propagate ``CellValue``; export may narrow that
hint when binding dtype metadata is available.

Pass 1 keeps a shared callee-hint map and merges each newly applied helper's
annotation via :func:`merge_callee_return_hints` so downstream units can
propagate types through schedule-ordered collapses. Runtime source for that
map commonly includes ``_readers.py`` (via ``_read_runtime_source``).
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence

ALLOWED_SCALAR_RETURN_TYPE_HINTS = frozenset({"bool", "float", "int", "str"})
ALLOWED_REFACTOR_RETURN_TYPE_HINTS = ALLOWED_SCALAR_RETURN_TYPE_HINTS | frozenset(
    {"CellValue"}
)

# Allowlisted runtime return annotations for inference and LLM stub sanitization.
KNOWN_RUNTIME_RETURN_HINTS: dict[str, str] = {
    "xl_compare": "bool",
    "xl_number": "float",
    "xl_cell": "CellValue",
    "xl_offset": "CellValue",
    "xl_range": "CellValue",
    "xl_range_rows": "CellValue",
    "xl_eval": "CellValue",
    "xl_circular_reference": "CellValue",
    "xl_match": "CellValue",
    "xl_index_ref": "CellValue",
}

_BINDING_DTYPE_TO_PYTHON: dict[str, str] = {
    "number": "float",
    "float": "float",
    "int": "int",
    "integer": "int",
    "string": "str",
    "str": "str",
    "boolean": "bool",
    "bool": "bool",
}

_RETURN_TYPE_ORDER = ("bool", "int", "float", "str", "CellValue")


class RefactorReturnTypeInferenceError(ValueError):
    """Raised when a helper return type cannot be inferred without guessing."""


def normalize_return_type_hint_for_allowlist(hint: str) -> str | None:
    """Project a return hint onto allowlisted refactor types, dropping forbidden parts."""
    parts = [part.strip() for part in hint.split("|")]
    allowed = [part for part in parts if part in ALLOWED_REFACTOR_RETURN_TYPE_HINTS]
    if not allowed:
        return None
    ordered = [name for name in _RETURN_TYPE_ORDER if name in allowed]
    return " | ".join(ordered)


def validate_scalar_return_type_hint(hint: str) -> None:
    parts = [part.strip() for part in hint.split("|")]
    for part in parts:
        if part not in ALLOWED_REFACTOR_RETURN_TYPE_HINTS:
            raise ValueError(f"unsupported return type hint: {hint!r}")


def format_return_type_hint(hints: set[str]) -> str:
    ordered = [name for name in _RETURN_TYPE_ORDER if name in hints]
    return " | ".join(ordered)


def _annotation_to_allowlisted_hint(annotation: ast.expr | None) -> str | None:
    if annotation is None:
        return None
    return normalize_return_type_hint_for_allowlist(ast.unparse(annotation).strip())


def function_return_hints_from_source(source: str) -> dict[str, str | None]:
    module = ast.parse(source)
    hints: dict[str, str | None] = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef):
            hints[node.name] = _annotation_to_allowlisted_hint(node.returns)
    return hints


def merge_callee_return_hints(
    hints: dict[str, str],
    *,
    source: str,
) -> dict[str, str]:
    """Merge allowlisted return annotations from ``source`` into ``hints`` in place."""
    for name, parsed in function_return_hints_from_source(source).items():
        if parsed is not None:
            hints[name] = parsed
    return hints


def merge_callee_return_hints_from_functions(
    hints: dict[str, str],
    functions: Mapping[str, ast.FunctionDef],
) -> dict[str, str]:
    """Merge return annotations from already-parsed defs into ``hints`` in place.

    Lets a long-running refactor pass refresh callee hints from a resealed
    ``InternalsSourceIndex`` without re-parsing the multi-megabyte module, so a
    later cluster whose member body is a bare call to a helper created earlier in
    the same pass can infer its return type instead of failing.
    """
    for name, node in functions.items():
        parsed = _annotation_to_allowlisted_hint(node.returns)
        if parsed is not None:
            hints[name] = parsed
    return hints


def build_callee_return_hints(
    *,
    runtime_source: str,
    internals_source: str,
) -> dict[str, str]:
    """Map callee names to allowlisted return hints from generated sources."""
    hints = dict(KNOWN_RUNTIME_RETURN_HINTS)
    for source in (runtime_source, internals_source):
        merge_callee_return_hints(hints, source=source)
    return hints


def _literal_type_hint(value: object) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return None


def _infer_expr_types(
    node: ast.expr,
    callees: Mapping[str, str],
) -> set[str] | None:
    if isinstance(node, ast.Constant):
        if node.value is None:
            # Empty IF branches compile to a literal None, a valid CellValue.
            return {"CellValue"}
        literal = _literal_type_hint(node.value)
        return {literal} if literal is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        callee_hint = callees.get(node.func.id)
        if callee_hint is None:
            return None
        # Callee maps store allowlisted unions as a single "float | CellValue"
        # string; expand to atomic parts so format_return_type_hint can order them.
        parts = {part.strip() for part in callee_hint.split("|") if part.strip()}
        return parts if parts else None
    if isinstance(node, ast.IfExp):
        body = _infer_expr_types(node.body, callees)
        orelse = _infer_expr_types(node.orelse, callees)
        if body is None or orelse is None:
            return None
        return body | orelse
    if isinstance(node, ast.BinOp):
        left = _infer_expr_types(node.left, callees)
        right = _infer_expr_types(node.right, callees)
        if left is None or right is None:
            return None
        combined = left | right
        if combined <= {"int"}:
            return {"int"}
        if combined <= {"int", "float"}:
            return {"float"}
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _infer_expr_types(node.operand, callees)
    return None


def _infer_function_return_types(
    source: str,
    callees: Mapping[str, str],
) -> set[str] | None:
    module = ast.parse(source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if not function_defs:
        return None
    hints: set[str] = set()
    for node in ast.walk(function_defs[0]):
        if isinstance(node, ast.Return):
            if node.value is None:
                return None
            expr_types = _infer_expr_types(node.value, callees)
            if expr_types is None:
                return None
            hints |= expr_types
    return hints if hints else None


def _binding_dtype_to_python(dtype: object) -> str | None:
    if not isinstance(dtype, str):
        return None
    return _BINDING_DTYPE_TO_PYTHON.get(dtype.strip().lower())


def _return_types_from_naming_hints(
    naming_hints: Mapping[str, object] | None,
) -> set[str] | None:
    if naming_hints is None:
        return None
    for key in ("return_dtype", "measure_dtype", "dtype"):
        mapped = _binding_dtype_to_python(naming_hints.get(key))
        if mapped is not None:
            return {mapped}
    binding_record = naming_hints.get("binding_record")
    if isinstance(binding_record, Mapping):
        mapped = _binding_dtype_to_python(binding_record.get("dtype"))
        if mapped is not None:
            return {mapped}
    return None


def narrow_return_type_hint_for_export(
    hint: str,
    naming_hints: Mapping[str, object] | None,
) -> str:
    """Narrow a purely opaque inferred hint using binding dtype metadata."""
    parts = {part.strip() for part in hint.split("|")}
    if parts != {"CellValue"}:
        return hint
    binding_hints = _return_types_from_naming_hints(naming_hints)
    if binding_hints is None:
        return hint
    return format_return_type_hint(binding_hints)


def infer_refactor_return_type_hint(
    *,
    python_sources: Sequence[str],
    runtime_source: str,
    internals_source: str,
    naming_hints: Mapping[str, object] | None = None,
    callee_hints: Mapping[str, str] | None = None,
) -> str:
    """Infer an allowlisted helper return hint from mechanical member bodies."""
    callees = (
        dict(callee_hints)
        if callee_hints is not None
        else build_callee_return_hints(
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
    )
    hints: set[str] = set()
    for _index, source in enumerate(python_sources):
        member_hints = _infer_function_return_types(source, callees)
        if member_hints is None:
            # A member whose body is a bare passthrough call to a helper whose
            # return annotation is not (yet) known falls back to the opaque
            # ``CellValue`` union rather than aborting the pass: the draft is
            # already value-verified, and ``CellValue`` is the safe supertype
            # (export can still narrow it via binding dtype hints).
            member_hints = _return_types_from_naming_hints(naming_hints) or {
                "CellValue"
            }
        hints |= member_hints
    if not hints:
        hints = _return_types_from_naming_hints(naming_hints) or {"CellValue"}
    result = narrow_return_type_hint_for_export(
        format_return_type_hint(hints),
        naming_hints,
    )
    validate_scalar_return_type_hint(result)
    return result
