"""Constant leaves and workbook-default input arrays."""

from __future__ import annotations

COUNTRY_PROFILE_NAMES: tuple[str, ...] = ('Borvelia', 'Litellia', 'Aurelium')

ENGINE_YEAR_LABELS: tuple[int | str, ...] = (1, 2, 3, 4, 5)

COUNTRY_NAME_DEFAULT: str = 'Borvelia'

COUNTRY_INITIAL_DEBT_DEFAULT: tuple[float | str, ...] = (60.0, 80.0, 40.0)

GROWTH_BASELINE_DEFAULT: tuple[float | str, ...] = (3.5, 3.5, 3.5, 3.5, 3.5)

INTEREST_BASELINE_DEFAULT: tuple[float | str, ...] = (4.0, 4.0, 4.0, 4.0, 4.0)

PRIMARY_BALANCE_BASELINE_DEFAULT: tuple[float | str, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0)

SHOCK_YEAR_DEFAULT: int | str = 2

SHOCK_TYPE_DEFAULT: int | str = 1

SHOCK_MAGNITUDES_DEFAULT: tuple[float | str, ...] = (-2.0, 2.0, -1.0)
