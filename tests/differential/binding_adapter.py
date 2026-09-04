"""Map inverted-tree ``compute_*`` calls to Excel cells via derived series."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from excel_grapher.core.address_keys import normalize_key

_RECORD_VALUE_FIELD = "OBS_VALUE"


def _cell_key(cell: Mapping[str, Any], key_fields: Sequence[str]) -> tuple[Any, ...]:
    key = cell["key"]
    return tuple(key[field] for field in key_fields)


def _record_key(
    record: Mapping[str, Any], key_fields: Sequence[str]
) -> tuple[Any, ...]:
    return tuple(record[field] for field in key_fields)


def _default_attr_name(parameter: str) -> str:
    return f"{parameter.upper()}_DEFAULT"


def expressible_input_cells(
    input_series: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """Normalized addresses of every bound input cell."""
    return frozenset(
        normalize_key(str(cell["address"]))
        for series in input_series
        for cell in series["cells"]
    )


def _record_sequence(value: object) -> Sequence[Mapping[str, Any]]:
    if not value:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(Sequence[Mapping[str, Any]], value)
    raise TypeError(f"expected a sequence of records, got {type(value).__name__}")


def overlay_series_values(
    series: Mapping[str, Any],
    default: Sequence[Any],
    records: Sequence[Mapping[str, Any]] = (),
) -> tuple[Any, ...]:
    """Return catalog-order values with sparse record overlays."""
    series_id = str(series["id"])
    cells = series["cells"]
    if len(default) != len(cells):
        raise ValueError(
            f"{series_id} default length {len(default)} does not match "
            f"{len(cells)} bound cells"
        )
    values = list(default)
    if not records:
        return tuple(values)
    key_fields = tuple(series["key_fields"])
    index = {_cell_key(cell, key_fields): i for i, cell in enumerate(cells)}
    for record in records:
        key = _record_key(record, key_fields)
        try:
            position = index[key]
        except KeyError as exc:
            raise LookupError(
                f"{series_id} has no cell for {dict(zip(key_fields, key, strict=True))}"
            ) from exc
        values[position] = record[_RECORD_VALUE_FIELD]
    return tuple(values)


def excel_writes_for_inputs(
    input_series: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Sparse Excel writes: every scalar, plus matrix cells named by records."""
    writes: dict[str, Any] = {}
    for series in input_series:
        series_id = str(series["id"])
        cells = series["cells"]
        key_fields = tuple(series["key_fields"])
        if not key_fields:
            if len(cells) != 1:
                raise ValueError(
                    f"{series_id} is a scalar series but has {len(cells)} cells"
                )
            if series_id not in inputs:
                raise TypeError(
                    f"scenario inputs are missing scalar series {series_id!r}"
                )
            writes[normalize_key(str(cells[0]["address"]))] = inputs[series_id]
            continue
        records = inputs.get(series_id) or ()
        index = {_cell_key(cell, key_fields): cell for cell in cells}
        for record in records:
            key = _record_key(record, key_fields)
            try:
                cell = index[key]
            except KeyError as exc:
                raise LookupError(
                    f"{series_id} has no cell for {dict(zip(key_fields, key, strict=True))}"
                ) from exc
            writes[normalize_key(str(cell["address"]))] = record[_RECORD_VALUE_FIELD]
    return writes


def input_kwargs_for_compute(
    function: Callable[..., object],
    data: object,
    *,
    inputs: Mapping[str, object],
    input_series: Sequence[Mapping[str, Any]] | None = None,
    scalar_input_keys: frozenset[str] | None = None,
) -> dict[str, object]:
    """Build keyword args for an inverted-tree ``compute_*`` function.

    When ``input_series`` is provided, matrix series overlay ``data.*_DEFAULT``
    arrays at catalog index. Otherwise dashboard scalars come from
    ``scalar_input_keys`` and required arrays come from ``data`` defaults.
    Constant kwargs that already have generated defaults are omitted.
    """
    series_by_id = (
        {str(series["id"]): series for series in input_series}
        if input_series is not None
        else {}
    )
    dashboard_keys = scalar_input_keys or frozenset()
    kwargs: dict[str, object] = {}
    function_name = getattr(function, "__name__", type(function).__name__)
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            raise TypeError(f"{function_name} has unsupported *args")
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            raise TypeError(f"{function_name} has unsupported **kwargs")
        series = series_by_id.get(name)
        if series is not None:
            if series["key_fields"]:
                attr = _default_attr_name(name)
                if not hasattr(data, attr):
                    module_name = getattr(data, "__name__", type(data).__name__)
                    raise TypeError(
                        f"{function_name} required parameter {name!r} needs "
                        f"{module_name}.{attr}"
                    )
                kwargs[name] = overlay_series_values(
                    series,
                    getattr(data, attr),
                    _record_sequence(inputs.get(name)),
                )
            else:
                if name not in inputs:
                    raise TypeError(
                        f"{function_name} required parameter {name!r} is a "
                        "bound input series but is missing from inputs"
                    )
                kwargs[name] = inputs[name]
            continue
        if name in dashboard_keys:
            if name not in inputs:
                raise TypeError(
                    f"{function_name} required parameter {name!r} is a "
                    "canonical dashboard input but is missing from inputs"
                )
            kwargs[name] = inputs[name]
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        attr = _default_attr_name(name)
        if not hasattr(data, attr):
            module_name = getattr(data, "__name__", type(data).__name__)
            raise TypeError(
                f"{function_name} required parameter {name!r} is not a "
                f"canonical dashboard input and {module_name} is missing {attr}"
            )
        kwargs[name] = getattr(data, attr)
    return kwargs
