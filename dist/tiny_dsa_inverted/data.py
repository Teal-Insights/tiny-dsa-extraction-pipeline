"""Constant leaves and workbook-default input arrays.

Constant series are compiled into this module. Public ``compute_*`` functions
still accept them as defaulted arguments so the leaf closure is visible in the
signature; callers override them only in tests.
"""

from __future__ import annotations

# Inputs!A10:A12 — MATCH lookup keys for the country profile table.
COUNTRY_PROFILE_NAMES: tuple[str, ...] = ("Borvelia", "Litellia", "Aurelium")

# Engine!C5:G5 — projection-year labels compared to shock_year.
ENGINE_YEAR_LABELS: tuple[int, ...] = (1, 2, 3, 4, 5)

# Mutable input defaults copied from the Tiny DSA workbook (Inputs sheet).
COUNTRY_NAME_DEFAULT: str = "Borvelia"
COUNTRY_INITIAL_DEBT_DEFAULT: tuple[float, ...] = (60.0, 80.0, 40.0)
GROWTH_BASELINE_DEFAULT: tuple[float, ...] = (3.5, 3.5, 3.5, 3.5, 3.5)
INTEREST_BASELINE_DEFAULT: tuple[float, ...] = (4.0, 4.0, 4.0, 4.0, 4.0)
PRIMARY_BALANCE_BASELINE_DEFAULT: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0)
SHOCK_YEAR_DEFAULT: int = 2
SHOCK_TYPE_DEFAULT: int = 1
SHOCK_MAGNITUDES_DEFAULT: tuple[float, ...] = (-2.0, 2.0, -1.0)
