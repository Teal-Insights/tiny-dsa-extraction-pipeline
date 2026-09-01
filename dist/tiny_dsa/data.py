"""Constant leaves and workbook-default input arrays."""

from __future__ import annotations

COUNTRY_PROFILE_NAMES: tuple[str, ...] = ('Borvelia', 'Litellia', 'Aurelium')

ENGINE_YEAR_LABELS: tuple[int, ...] = (1, 2, 3, 4, 5)

COUNTRY_NAME_DEFAULT: str = 'Borvelia'

COUNTRY_INITIAL_DEBT_DEFAULT: tuple[float, ...] = (60.0, 80.0, 40.0)

GROWTH_BASELINE_DEFAULT: tuple[float, ...] = (3.5, 3.5, 3.5, 3.5, 3.5)

INTEREST_BASELINE_DEFAULT: tuple[float, ...] = (4.0, 4.0, 4.0, 4.0, 4.0)

PRIMARY_BALANCE_BASELINE_DEFAULT: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0)

SHOCK_YEAR_DEFAULT: int = 2

SHOCK_TYPE_DEFAULT: int = 1

SHOCK_MAGNITUDES_DEFAULT: tuple[float, ...] = (-2.0, 2.0, -1.0)
