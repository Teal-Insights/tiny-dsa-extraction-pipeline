"""Soft-error rewrite helpers for series-binding ``compute_*`` functions.

Upstream codegen (excel-grapher ``compute_codegen.emit_compute_function``)
fills each leaf measure via ``xl_cell(ctx, address)``, which raises
``XlErrorException`` on Excel error codes and aborts the entire series on the
first bad historical year. excel-grapher 3.17+ emits soft-capture natively
(Teal-Insights/excel-grapher#436): generated measure evaluation catches
``XlErrorException``, stores ``exc.code`` on the measure field, and continues
the series. These helpers remain for rewriting older cached ``api.py``
payloads that still assign ``xl_cell(ctx, address)`` without a try/except.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_MEASURE_ASSIGN = "        record[measure_field] = xl_cell(ctx, address)"
_RUNTIME_IMPORT_PAREN_RE = re.compile(
    r"^from \.runtime import \((?P<paren>.*?)^\)$",
    re.MULTILINE | re.DOTALL,
)
_RUNTIME_IMPORT_FLAT_RE = re.compile(
    r"^from \.runtime import (?P<flat>[^(\n][^\n]*)$",
    re.MULTILINE,
)


def rewrite_compute_measure_assignment(lines: Sequence[str]) -> list[str]:
    """Rewrite bare ``xl_cell`` measure assignments into try/except soft errors."""
    out: list[str] = []
    for line in lines:
        if line == _MEASURE_ASSIGN:
            out.extend(
                [
                    "        try:",
                    "            record[measure_field] = xl_cell(ctx, address)",
                    "        except XlErrorException as _xl_err:",
                    "            record[measure_field] = _xl_err.code",
                ]
            )
        else:
            out.append(line)
    return out


def _parse_import_names(raw_names: Sequence[str]) -> list[str]:
    """Normalize import-name fragments to bare identifiers."""
    names: list[str] = []
    for part in raw_names:
        name = part.strip().strip(",").strip()
        if name.isidentifier():
            names.append(name)
    return names


def _ordered_import_names(names: Sequence[str]) -> list[str]:
    """Insert ``XlErrorException`` after ``EvalContext`` when present."""
    cleaned = [name for name in names if name != "XlErrorException"]
    if "EvalContext" in cleaned:
        insert_at = cleaned.index("EvalContext") + 1
        cleaned.insert(insert_at, "XlErrorException")
        return cleaned
    return sorted([*cleaned, "XlErrorException"])


def ensure_xl_error_exception_import(source: str) -> str:
    """Ensure ``XlErrorException`` is imported from ``.runtime`` in ``api.py``."""
    paren_match = _RUNTIME_IMPORT_PAREN_RE.search(source)
    if paren_match is not None:
        names = _parse_import_names(paren_match.group("paren").splitlines())
        ordered = _ordered_import_names(names)
        replacement = (
            "from .runtime import (\n"
            + "".join(f"    {name},\n" for name in ordered)
            + ")"
        )
        if paren_match.group(0) == replacement:
            return source
        return _RUNTIME_IMPORT_PAREN_RE.sub(replacement, source, count=1)

    flat_match = _RUNTIME_IMPORT_FLAT_RE.search(source)
    if flat_match is None:
        raise ValueError("api.py is missing a `.runtime` import to extend")
    names = _parse_import_names(flat_match.group("flat").split(","))
    ordered = _ordered_import_names(names)
    replacement = f"from .runtime import {', '.join(ordered)}"
    if flat_match.group(0) == replacement:
        return source
    return _RUNTIME_IMPORT_FLAT_RE.sub(replacement, source, count=1)
