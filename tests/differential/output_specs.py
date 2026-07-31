"""Derived-series output specs for the exported-library differential harness."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OutputCellSpec:
    """One bound output cell compared by the exported-library differential."""

    label: str
    address: str
    compute: str
    keys: tuple[tuple[str, Any], ...]


def specs_from_output_series(
    output_series: Sequence[Mapping[str, Any]],
) -> tuple[OutputCellSpec, ...]:
    """Build one spec per bound output cell from derived output-series payloads."""
    specs: list[OutputCellSpec] = []
    for series in output_series:
        series_id = str(series["id"])
        compute = str(series["compute_name"])
        key_fields = tuple(series["key_fields"])
        for cell in series["cells"]:
            key_values = tuple((field, cell["key"][field]) for field in key_fields)
            suffix = ",".join(str(value) for _, value in key_values)
            label = f"{series_id}[{suffix}]" if suffix else series_id
            specs.append(
                OutputCellSpec(
                    label=label,
                    address=str(cell["address"]),
                    compute=compute,
                    keys=key_values,
                )
            )
    return tuple(specs)


def outputs_from_records(
    specs: tuple[OutputCellSpec, ...],
    records_by_compute: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Key-match compute_* records onto specs; missing/ambiguous → None.

    Specs whose ``(compute, keys)`` pair is shared by more than one spec are
    inherently ambiguous — several bound series collide on one generated
    function name with identical key shapes — so no record can be trusted to
    belong to any one of them. They resolve to ``None`` deterministically
    instead of silently receiving whichever series' records the surviving
    function returns.
    """
    indexed: dict[str, dict[tuple[tuple[str, Any], ...], Any]] = {}
    for compute, records in records_by_compute.items():
        spec_keys_for_compute = [spec.keys for spec in specs if spec.compute == compute]
        fields = (
            tuple(field for field, _ in spec_keys_for_compute[0])
            if spec_keys_for_compute
            else ()
        )
        indexed[compute] = {
            tuple((field, record.get(field)) for field in fields): record.get(
                "OBS_VALUE"
            )
            for record in records
        }
    ambiguity = Counter((spec.compute, spec.keys) for spec in specs)
    return {
        spec.label: (
            None
            if ambiguity[(spec.compute, spec.keys)] > 1
            else indexed.get(spec.compute, {}).get(spec.keys)
        )
        for spec in specs
    }
