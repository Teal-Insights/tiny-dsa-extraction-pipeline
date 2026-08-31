"""Workbook address parsing helpers."""

from __future__ import annotations


def parse_workbook_address(address: str) -> tuple[str, str, int]:
    sheet, colrow = address.split("!", 1)
    column = "".join(character for character in colrow if character.isalpha())
    row = int("".join(character for character in colrow if character.isdigit()))
    return sheet, column, row
