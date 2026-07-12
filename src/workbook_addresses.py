"""Workbook address parsing and optional projection-column layout helpers."""

from __future__ import annotations

from dataclasses import dataclass


def parse_workbook_address(address: str) -> tuple[str, str, int]:
    sheet, colrow = address.split("!", 1)
    column = "".join(character for character in colrow if character.isalpha())
    row = int("".join(character for character in colrow if character.isdigit()))
    return sheet, column, row


@dataclass(frozen=True)
class ProjectionColumnLayout:
    """Optional layout for workbooks with parallel time-series columns."""

    engine_sheet: str
    engine_columns: tuple[str, ...]
    outputs_sheet: str
    outputs_column_to_engine: dict[str, str]
    time_period_to_engine_column: dict[int, str]
    time_period_header_row: int = 5
    projection_dimension_id: str = "TIME_PERIOD"

    def logical_engine_column(self, address: str) -> str | None:
        sheet, column, _row = parse_workbook_address(address)
        engine_column_set = frozenset(self.engine_columns)
        if sheet == self.engine_sheet and column in engine_column_set:
            return column
        if sheet == self.outputs_sheet:
            return self.outputs_column_to_engine.get(column)
        return None

    def engine_column_for_address(self, address: str) -> str | None:
        return self.logical_engine_column(address)

    def time_period_for_engine_column(self, column: str) -> int | None:
        for time_period, engine_column in self.time_period_to_engine_column.items():
            if engine_column == column:
                return time_period
        return None
