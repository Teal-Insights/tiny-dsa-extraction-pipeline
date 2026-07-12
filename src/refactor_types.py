"""Shared types for internals refactor and formula clustering."""

from __future__ import annotations

from typing import Literal, TypeAlias, cast, get_args

VariationMode: TypeAlias = Literal["independent", "dominant_key_only"]

_VARIATION_MODE_CHOICES: tuple[str, ...] = get_args(VariationMode)

VARIATION_MODE_CLI_HELP = (
    "Formula-cluster variation mode for internals refactor clustering "
    '(default: workbook_config.VARIATION_MODE or "independent"). '
    "Only affects export and refactor-bucket stages, not --extract-graph."
)


def variation_mode_choices() -> tuple[str, ...]:
    return _VARIATION_MODE_CHOICES


def parse_variation_mode(value: object) -> VariationMode:
    if value not in _VARIATION_MODE_CHOICES:
        raise ValueError(
            "VARIATION_MODE must be one of "
            f"{list(_VARIATION_MODE_CHOICES)}; got {value!r}"
        )
    return cast(VariationMode, value)
