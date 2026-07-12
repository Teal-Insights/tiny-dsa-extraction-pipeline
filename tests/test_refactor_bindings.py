"""Unit tests for dimension-id-first binding key resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.refactor_bindings import (
    KeyConceptSpec,
    build_bound_address_keys,
    engine_column_from_member_keys,
    expected_member_keys_for_cluster,
    helper_parameters_for_varying_keys,
    load_key_concept_vocabulary,
    resolve_dimension_key,
    varying_key_concepts,
)
from src.workbook_addresses import ProjectionColumnLayout

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHARED_TIME_PERIOD_BINDINGS = FIXTURES / "bindings_shared_time_period"


def test_load_key_concept_vocabulary_keeps_distinct_ids_for_shared_concept() -> None:
    vocabulary = load_key_concept_vocabulary(SHARED_TIME_PERIOD_BINDINGS)
    by_id = {item.dimension_id: item for item in vocabulary}
    assert set(by_id) == {"PROJECTION_PERIOD", "REFERENCE_PERIOD"}
    assert by_id["PROJECTION_PERIOD"].concept == "TIME_PERIOD"
    assert by_id["REFERENCE_PERIOD"].concept == "TIME_PERIOD"
    assert by_id["PROJECTION_PERIOD"].suggested_param_name == "projection_period"
    assert by_id["REFERENCE_PERIOD"].suggested_param_name == "reference_period"


def test_load_key_concept_vocabulary_falls_back_to_concept_when_id_omitted(
    tmp_path: Path,
) -> None:
    bindings = tmp_path / "bindings"
    bindings.mkdir()
    (bindings / "internals.bindings.yaml").write_text(
        """
schema_version: 1.8.0
workbook: workbook.xlsx
concept_scheme:
  id: legacy
  concepts:
    - id: TIME_PERIOD
      name: Time period
      dtype: int
    - id: OBS_VALUE
      name: Observation value
      dtype: number
series:
  - id: legacy_row
    sheet: Engine
    data_range: Engine!C10:D10
    layout: row_series
    internal: {}
    structure:
      measure:
        concept: OBS_VALUE
        dtype: float
        bind:
          kind: data_cell
          read: float
      dimensions:
        - concept: TIME_PERIOD
          role: key
          scope: cell
          bind:
            kind: column_header
            header_row: 5
            read: int
    key: [TIME_PERIOD]
""".strip(),
        encoding="utf-8",
    )
    vocabulary = load_key_concept_vocabulary(bindings)
    assert len(vocabulary) == 1
    assert vocabulary[0].dimension_id == "TIME_PERIOD"
    assert vocabulary[0].concept == "TIME_PERIOD"
    assert vocabulary[0].suggested_param_name == "time_period"


def test_resolve_dimension_key_prefers_id_and_falls_back_to_unique_concept() -> None:
    vocabulary = (
        KeyConceptSpec(
            dimension_id="PROJECTION_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="projection_period",
        ),
        KeyConceptSpec(
            dimension_id="REF_AREA",
            concept="REF_AREA",
            dtype="string",
            suggested_param_name="ref_area",
        ),
    )
    assert resolve_dimension_key("PROJECTION_PERIOD", vocabulary) == "PROJECTION_PERIOD"
    assert resolve_dimension_key("REF_AREA", vocabulary) == "REF_AREA"


def test_resolve_dimension_key_rejects_ambiguous_shared_concept() -> None:
    vocabulary = (
        KeyConceptSpec(
            dimension_id="PROJECTION_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="projection_period",
        ),
        KeyConceptSpec(
            dimension_id="REFERENCE_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="reference_period",
        ),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_dimension_key("TIME_PERIOD", vocabulary)


def test_build_bound_address_keys_preserves_effective_ids() -> None:
    bound = build_bound_address_keys(
        (),
        (),
        (
            {
                "cells": [
                    {
                        "address": "Engine!C10",
                        "key": {
                            "PROJECTION_PERIOD": 1,
                            "REFERENCE_PERIOD": 0,
                        },
                    }
                ]
            },
        ),
    )
    assert bound["Engine!C10"] == {
        "PROJECTION_PERIOD": 1,
        "REFERENCE_PERIOD": 0,
    }


def test_varying_and_expected_member_keys_use_dimension_ids(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "workbook.xlsx"
    workbook.write_bytes(b"")
    bound = {
        "Engine!C10": {"PROJECTION_PERIOD": 1, "REFERENCE_PERIOD": 0},
        "Engine!D10": {"PROJECTION_PERIOD": 2, "REFERENCE_PERIOD": 0},
    }
    varying = varying_key_concepts(
        ("Engine!C10", "Engine!D10"),
        bound_address_keys=bound,
        workbook_path=workbook,
    )
    assert varying == frozenset({"PROJECTION_PERIOD"})
    expected = expected_member_keys_for_cluster(
        ("Engine!C10", "Engine!D10"),
        bound_address_keys=bound,
        workbook_path=workbook,
    )
    assert expected == {
        "Engine!C10": {"PROJECTION_PERIOD": 1},
        "Engine!D10": {"PROJECTION_PERIOD": 2},
    }


def test_engine_column_from_member_keys_uses_projection_dimension_id() -> None:
    layout = ProjectionColumnLayout(
        engine_sheet="Engine",
        engine_columns=("C", "D"),
        outputs_sheet="Outputs",
        outputs_column_to_engine={},
        time_period_to_engine_column={1: "C", 2: "D"},
        projection_dimension_id="PROJECTION_PERIOD",
    )
    assert (
        engine_column_from_member_keys(
            {"PROJECTION_PERIOD": 2, "REFERENCE_PERIOD": 0},
            address="Engine!D10",
            layout=layout,
        )
        == "D"
    )


def test_engine_column_from_member_keys_falls_back_to_time_period_concept() -> None:
    layout = ProjectionColumnLayout(
        engine_sheet="Engine",
        engine_columns=("C", "D"),
        outputs_sheet="Outputs",
        outputs_column_to_engine={},
        time_period_to_engine_column={1: "C", 2: "D"},
        projection_dimension_id="PROJECTION_PERIOD",
    )
    assert (
        engine_column_from_member_keys(
            {"TIME_PERIOD": 1},
            address="Engine!C10",
            layout=layout,
        )
        == "C"
    )


def test_helper_parameters_for_varying_keys_indexes_by_dimension_id() -> None:
    vocabulary = (
        KeyConceptSpec(
            dimension_id="PROJECTION_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="projection_period",
        ),
        KeyConceptSpec(
            dimension_id="REFERENCE_PERIOD",
            concept="TIME_PERIOD",
            dtype="int",
            suggested_param_name="reference_period",
        ),
    )
    params = helper_parameters_for_varying_keys(
        frozenset({"PROJECTION_PERIOD", "REFERENCE_PERIOD"}),
        vocabulary,
    )
    assert [item.dimension_id for item in params] == [
        "PROJECTION_PERIOD",
        "REFERENCE_PERIOD",
    ]
