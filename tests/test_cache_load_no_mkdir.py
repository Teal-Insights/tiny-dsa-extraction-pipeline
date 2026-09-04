"""Cache load helpers must not create directories on a miss."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from src.bindings_validation_cache import load_bindings_validation_report
from src.codegen_cache import load_codegen_payload
from src.graph_cache import load_dependency_graph
from src.projection_cache import load_projection_payload
from src.series_derived_cache import load_series_derived_payload
from src.series_resolution_cache import load_series_resolution_payload

_CACHE_KEY = "0" * 64

_LOADERS: tuple[tuple[str, Callable[..., object | None]], ...] = (
    ("bindings_validation", load_bindings_validation_report),
    ("codegen", load_codegen_payload),
    ("dependency_graph", load_dependency_graph),
    ("projection", load_projection_payload),
    ("series_derived", load_series_derived_payload),
    ("series_resolution", load_series_resolution_payload),
)


@pytest.mark.parametrize(
    "load_fn",
    [pytest.param(load_fn, id=label) for label, load_fn in _LOADERS],
)
def test_load_miss_does_not_create_cache_dir(
    tmp_path: Path,
    load_fn: Callable[..., object | None],
) -> None:
    cache_dir = tmp_path / "absent-cache"
    assert not cache_dir.exists()
    assert load_fn(_CACHE_KEY, cache_dir=cache_dir) is None
    assert not cache_dir.exists()
