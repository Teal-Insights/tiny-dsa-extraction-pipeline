"""Constant leaves, key domains, and workbook-default input arrays."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

COUNTRY_DOMAIN: tuple[str, ...] = ('Borvelia', 'Litellia', 'Aurelium')

TIME_PERIOD_DOMAIN: tuple[int, ...] = (1, 2, 3, 4, 5)

SHOCK_PARAMETER_DOMAIN: tuple[str, ...] = ('Growth', 'Interest', 'Primary balance')

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

_CONSTANT_NAMES = frozenset({'COUNTRY_PROFILE_NAMES', 'ENGINE_YEAR_LABELS'})


@contextmanager
def overrides(**values: object) -> Iterator[None]:
    """Replace constant attributes for the duration of the `with` block.

    Args:
        **values: Mapping of this module's constant names to replacements.
            Unknown names raise `AttributeError`.
    """
    unknown = [name for name in values if name not in _CONSTANT_NAMES]
    if unknown:
        joined = ", ".join(repr(name) for name in unknown)
        raise AttributeError("unknown constant(s): " + joined)
    namespace = globals()
    saved = {name: namespace[name] for name in values}
    namespace.update(values)
    try:
        yield
    finally:
        namespace.update(saved)
