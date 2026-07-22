"""Minimal runtime module fixture for symbol-discovery tests."""

from __future__ import annotations

from enum import StrEnum


class XlError(StrEnum):
    VALUE = "#VALUE!"


def to_bool(value: object) -> bool | XlError:
    return True


def to_int(value: object) -> int | XlError:
    return 0


def to_number(value: object) -> float | XlError:
    return 0.0


def compare_scalars(op: str, left: object, right: object) -> bool | XlError:
    return True


def xl_add(left: float, right: float) -> float:
    return left + right


def xl_cell(ctx: object, address: str) -> object:
    return address
