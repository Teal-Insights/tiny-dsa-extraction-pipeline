"""Tiny DSA projection-column layout: Engine C–G years and Outputs offset mapping."""

from __future__ import annotations

from typing import Literal

ENGINE_COLUMNS: tuple[str, ...] = ("C", "D", "E", "F", "G")
ENGINE_COLUMN_SET = frozenset(ENGINE_COLUMNS)
OUTPUTS_COLUMN_TO_ENGINE = {"B": "C", "C": "D", "D": "E", "E": "F", "F": "G"}

EngineColumn = Literal["C", "D", "E", "F", "G"]

TIME_PERIOD_TO_ENGINE_COLUMN: dict[int, EngineColumn] = {
    1: "C",
    2: "D",
    3: "E",
    4: "F",
    5: "G",
}

_ENGINE_COLUMN_BY_LETTER: dict[str, EngineColumn] = {
    "C": "C",
    "D": "D",
    "E": "E",
    "F": "F",
    "G": "G",
}


def parse_workbook_address(address: str) -> tuple[str, str, int]:
    sheet, colrow = address.split("!", 1)
    column = "".join(character for character in colrow if character.isalpha())
    row = int("".join(character for character in colrow if character.isdigit()))
    return sheet, column, row


def logical_engine_column(address: str) -> str | None:
    """Map a workbook address to its Engine-sheet projection column, if any."""
    sheet, column, _row = parse_workbook_address(address)
    if sheet == "Engine" and column in ENGINE_COLUMN_SET:
        return column
    if sheet == "Outputs":
        return OUTPUTS_COLUMN_TO_ENGINE.get(column)
    return None


def engine_column_for_address(address: str) -> EngineColumn | None:
    """Return the typed engine column for a projection-column address, if any."""
    column = logical_engine_column(address)
    if column is None:
        return None
    return _ENGINE_COLUMN_BY_LETTER.get(column)
