"""Helpers for OptimalCompression preserve sets."""

from __future__ import annotations

from typing import cast

from excel_grapher.series_bindings.normalize import (
    has_constant_direction,
    has_input_direction,
    has_output_direction,
)
from excel_grapher.series_bindings.types import WorkbookSeriesBindings


def public_series_bindings_for_preserve(
    bindings: WorkbookSeriesBindings,
) -> WorkbookSeriesBindings:
    """Drop internal series so OptimalCompression can inline singleton transit cells.

    Preserve only input / output / constant addresses (typically public API leaves
    that are not export targets). Internals remain eligible for compression.
    """
    series = [
        entry
        for entry in bindings.get("series", [])
        if isinstance(entry, dict)
        and (
            has_input_direction(entry)
            or has_output_direction(entry)
            or has_constant_direction(entry)
        )
    ]
    return cast(WorkbookSeriesBindings, {**dict(bindings), "series": series})
