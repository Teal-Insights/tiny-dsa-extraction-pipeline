from __future__ import annotations

from pathlib import Path

import pytest

from src.refactor_bindings import (
    BindingKeyValue,
    build_bound_address_keys,
    expected_keys_for_address,
    expected_member_keys_for_cluster,
    load_key_concept_vocabulary,
    varying_key_concepts,
)
import workbook_config

BINDINGS_PATH = Path("bindings")
WORKBOOK_PATH = workbook_config.WORKBOOK_PATH
PROJECTION_LAYOUT = workbook_config.PROJECTION_LAYOUT


@pytest.fixture(scope="module")
def bound_address_keys(
    tiny_dsa_configured_pipeline,
) -> dict[str, dict[str, BindingKeyValue]]:
    pipeline = tiny_dsa_configured_pipeline
    return build_bound_address_keys(pipeline.input_series, pipeline.output_series)


def test_load_key_concept_vocabulary_includes_cell_scoped_keys() -> None:
    vocabulary = load_key_concept_vocabulary(BINDINGS_PATH)
    concepts = {item.concept for item in vocabulary}
    assert "TIME_PERIOD" in concepts
    assert "COUNTRY" in concepts
    assert "SHOCK_PARAMETER" in concepts
    assert "PARAMETER" not in concepts


def test_expected_keys_for_engine_row_use_time_period(
    bound_address_keys: dict[str, dict[str, BindingKeyValue]],
) -> None:
    assert expected_keys_for_address(
        "Engine!D10",
        bound_address_keys=bound_address_keys,
        workbook_path=WORKBOOK_PATH,
        layout=PROJECTION_LAYOUT,
    ) == {"TIME_PERIOD": 2}


def test_expected_keys_for_output_row_use_binding_resolution(
    bound_address_keys: dict[str, dict[str, BindingKeyValue]],
) -> None:
    keys = expected_keys_for_address(
        "Outputs!C14",
        bound_address_keys=bound_address_keys,
        workbook_path=WORKBOOK_PATH,
    )
    assert keys["TIME_PERIOD"] == 2


def test_varying_key_concepts_for_engine_row_10(
    bound_address_keys: dict[str, dict[str, BindingKeyValue]],
) -> None:
    addresses = (
        "Engine!C10",
        "Engine!D10",
        "Engine!E10",
        "Engine!F10",
        "Engine!G10",
    )
    assert varying_key_concepts(
        addresses,
        bound_address_keys=bound_address_keys,
        workbook_path=WORKBOOK_PATH,
        layout=PROJECTION_LAYOUT,
    ) == frozenset({"TIME_PERIOD"})


def test_expected_member_keys_for_engine_row_10(
    bound_address_keys: dict[str, dict[str, BindingKeyValue]],
) -> None:
    expected = expected_member_keys_for_cluster(
        (
            "Engine!C10",
            "Engine!D10",
            "Engine!E10",
            "Engine!F10",
            "Engine!G10",
        ),
        bound_address_keys=bound_address_keys,
        workbook_path=WORKBOOK_PATH,
        layout=PROJECTION_LAYOUT,
    )
    assert expected["Engine!C10"] == {"TIME_PERIOD": 1}
    assert expected["Engine!D10"] == {"TIME_PERIOD": 2}
    assert expected["Engine!G10"] == {"TIME_PERIOD": 5}
