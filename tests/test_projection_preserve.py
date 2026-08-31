from __future__ import annotations

from src.projection_preserve import public_series_bindings_for_preserve
from tests.fixtures.synthetic_pipeline import load_synthetic_series_bindings


def test_public_series_bindings_for_preserve_drops_internals() -> None:
    bindings = load_synthetic_series_bindings()
    public = public_series_bindings_for_preserve(bindings)

    assert [entry["id"] for entry in public["series"]] == [
        "input_bias",
        "input_rate",
        "result_a",
        "result_b",
    ]
    assert {entry["id"] for entry in bindings["series"]} >= {
        "engine_b2",
        "engine_c2",
        "input_bias",
        "input_rate",
        "result_a",
        "result_b",
    }
