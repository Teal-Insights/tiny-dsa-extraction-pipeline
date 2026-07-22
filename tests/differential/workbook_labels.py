"""Workbook-exact dropdown label resolution for differential harnesses.

Scenario matrices and exported APIs use clean logical values (``"High"``,
``"Real interest rate"``). Workbooks often branch with exact string equality on
reference label cells. Harnesses must map logical scenario values to the exact
workbook literals before writing public input cells.

See ``technical_standard.md`` (Configure stage) and ``tests/differential/README.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, get_args, get_origin

from excel_grapher.core.cell_types import normalize_cell_type_env_key


def resolve_public_input_value(logical: str, choices: Sequence[str]) -> str:
    """Map a logical scenario value to an exact workbook reference label.

    Matching rules (in order):

    1. Exact membership in ``choices``.
    2. Strip-insensitive match — returns the raw workbook string.
    3. Prefix match — logical is a prefix of exactly one choice (handles suffix
       variants like ``"Real interest rate"`` → ``"Real interest rate (a)"``).

    Raises ``ValueError`` when no unique match exists.
    """
    if logical in choices:
        return logical

    stripped_matches = [
        choice for choice in choices if choice.strip() == logical.strip()
    ]
    if len(stripped_matches) == 1:
        return stripped_matches[0]
    if len(stripped_matches) > 1:
        raise ValueError(
            f"ambiguous stripped match for {logical!r} among {stripped_matches!r}"
        )

    prefix_matches = [
        choice for choice in choices if choice.startswith(logical.rstrip())
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise ValueError(
            f"ambiguous prefix match for {logical!r} among {prefix_matches!r}"
        )

    raise ValueError(f"no workbook label match for {logical!r} among {list(choices)!r}")


def literal_constraint_values(constraint: object) -> tuple[str, ...]:
    """Return ``Literal[...]`` string members from a leaf constraint, if any."""
    origin = get_origin(constraint)
    if origin is not Annotated:
        return ()
    args = get_args(constraint)
    if len(args) < 2:
        return ()
    literal_origin = get_origin(args[1])
    if literal_origin is not Literal:
        return ()
    literal_args = get_args(args[1])
    return tuple(str(value) for value in literal_args)


def validate_reference_labels(
    *,
    reference_labels: Mapping[str, Sequence[str]],
    constraint_literals: Mapping[str, Sequence[str]],
    scenario_values: Mapping[str, Sequence[str]],
) -> list[str]:
    """Return human-readable configure failures for label resolution."""
    failures: list[str] = []

    for input_key, literals in constraint_literals.items():
        labels = reference_labels.get(input_key)
        if labels is None:
            failures.append(
                f"missing reference labels for constrained input {input_key!r}"
            )
            continue
        label_set = set(labels)
        for literal in literals:
            if literal not in label_set:
                failures.append(
                    f"CONSTRAINTS literal {literal!r} for {input_key!r} "
                    f"not among reference labels {list(labels)!r}"
                )

    for input_key, values in scenario_values.items():
        labels = reference_labels.get(input_key)
        if labels is None:
            failures.append(
                f"missing reference labels for scenario input {input_key!r}"
            )
            continue
        for value in values:
            try:
                resolve_public_input_value(value, labels)
            except ValueError as exc:
                failures.append(f"scenario value {value!r} for {input_key!r}: {exc}")

    return failures


def normalize_input_key(input_key: str) -> str:
    """Normalize a sheet-qualified input address for config lookups."""
    return normalize_cell_type_env_key(input_key)
