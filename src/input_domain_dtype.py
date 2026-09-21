"""Fail closed when ``input.domain`` kind does not match measure dtype.

excel-grapher ``between`` is an integer interval (``isinstance(value, int)``).
``real_between`` is the real interval. Inverted-tree codegen copies the authored
domain into ``compute_*`` and emits ``data.py`` defaults from the measure dtype,
so ``dtype: float`` plus ``between`` exports a library that rejects its own
workbook defaults (``0.0 not in between(min=0, max=1)``).

``validate_series_bindings`` historically accepted this pairing and export
succeeded, so derived pipelines reject it at binding load. Once excel-grapher
covers the check, this local gate can be deleted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from excel_grapher.series_bindings.types import WorkbookSeriesBindings

_INTEGER_DTYPES = frozenset({"int", "integer"})
_REAL_DTYPES = frozenset({"float", "number"})
_NUMERIC_DOMAIN_KINDS = frozenset({"between", "real_between"})


def _concept_dtype_index(bindings: Mapping[str, Any]) -> dict[str, str]:
    scheme = bindings.get("concept_scheme")
    if not isinstance(scheme, Mapping):
        return {}
    concepts = scheme.get("concepts")
    if not isinstance(concepts, Sequence) or isinstance(concepts, (str, bytes)):
        return {}
    index: dict[str, str] = {}
    for concept in concepts:
        if not isinstance(concept, Mapping):
            continue
        concept_id = concept.get("id")
        dtype = concept.get("dtype")
        if (
            isinstance(concept_id, str)
            and concept_id
            and isinstance(dtype, str)
            and dtype
        ):
            index[concept_id] = dtype
    return index


def _measure_dtype(
    series: Mapping[str, Any], concept_dtypes: Mapping[str, str]
) -> str | None:
    structure = series.get("structure")
    if not isinstance(structure, Mapping):
        return None
    measure = structure.get("measure")
    if not isinstance(measure, Mapping):
        return None
    dtype = measure.get("dtype")
    if isinstance(dtype, str) and dtype:
        return dtype
    concept = measure.get("concept")
    if isinstance(concept, str) and concept:
        return concept_dtypes.get(concept)
    return None


def _input_domain_kind(series: Mapping[str, Any]) -> str | None:
    input_block = series.get("input")
    if not isinstance(input_block, Mapping):
        return None
    domain = input_block.get("domain")
    if not isinstance(domain, Mapping):
        return None
    if "enum" in domain:
        return "enum"
    if "between" in domain:
        return "between"
    if "real_between" in domain:
        return "real_between"
    return None


def _domain_matches_dtype(kind: str, dtype: str | None) -> bool:
    if kind == "enum":
        return True
    if kind not in _NUMERIC_DOMAIN_KINDS:
        return True
    if dtype is None:
        return False
    if kind == "between":
        return dtype in _INTEGER_DTYPES
    return dtype in _REAL_DTYPES


def input_domain_dtype_mismatches(
    bindings: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return input series ids whose numeric domain does not match measure dtype."""
    concept_dtypes = _concept_dtype_index(bindings)
    series_list = bindings.get("series")
    if not isinstance(series_list, Sequence) or isinstance(series_list, (str, bytes)):
        return ()
    mismatched: list[str] = []
    for series in series_list:
        if not isinstance(series, Mapping):
            continue
        kind = _input_domain_kind(series)
        if kind is None:
            continue
        dtype = _measure_dtype(series, concept_dtypes)
        if _domain_matches_dtype(kind, dtype):
            continue
        series_id = series.get("id")
        if isinstance(series_id, str) and series_id:
            mismatched.append(series_id)
    return tuple(mismatched)


def require_input_domain_dtype_consistency(
    bindings: WorkbookSeriesBindings | Mapping[str, Any],
) -> None:
    """Raise ``ValueError`` listing input series with domain/dtype mismatches."""
    mismatched = input_domain_dtype_mismatches(bindings)
    if not mismatched:
        return
    raise ValueError(
        "input.domain kind does not match measure dtype for series: "
        + ", ".join(mismatched)
        + " (between is integer-only; real_between is required for float/number)"
    )
