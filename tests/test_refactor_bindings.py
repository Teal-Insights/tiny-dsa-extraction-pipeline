"""Unit tests for dimension-id-first binding key resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.refactor_bindings import (
    KeyConceptSpec,
    build_address_to_series_id,
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
schema_version: 1.10.0
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


def test_build_bound_address_keys_includes_keyed_constant_series() -> None:
    # A reader-only leaf (constant series) that still carries a per-cell key must
    # contribute that key so ref slots pointing at it record TIME_PERIOD rather
    # than an empty dict.
    constant_series = [
        {
            "cells": [
                {"address": "Inflation!AA8", "key": {"TIME_PERIOD": 2027}},
                {"address": "Inflation!AB8", "key": {"TIME_PERIOD": 2028}},
            ]
        }
    ]
    bound = build_bound_address_keys((), (), (), constant_series=constant_series)
    assert bound["Inflation!AA8"] == {"TIME_PERIOD": 2027}
    assert bound["Inflation!AB8"] == {"TIME_PERIOD": 2028}


def test_build_bound_address_keys_internal_overrides_constant_on_shared_address() -> (
    None
):
    constant_series = [
        {"cells": [{"address": "Engine!C10", "key": {"TIME_PERIOD": 1999}}]}
    ]
    internal_series = [
        {"cells": [{"address": "Engine!C10", "key": {"TIME_PERIOD": 2000}}]}
    ]
    bound = build_bound_address_keys(
        (), (), internal_series, constant_series=constant_series
    )
    assert bound["Engine!C10"] == {"TIME_PERIOD": 2000}


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


def test_build_address_to_series_id_maps_internal_series_cells() -> None:
    internal_series = [
        {
            "id": "revenue_growth",
            "cells": [
                {"address": "Engine!B5", "key": {}},
                {"address": "Engine!C5", "key": {}},
            ],
        },
        {
            "id": "expenditure_growth",
            "cells": [{"address": "Engine!B6", "key": {}}],
        },
    ]

    assert build_address_to_series_id(internal_series) == {
        "Engine!B5": "revenue_growth",
        "Engine!C5": "revenue_growth",
        "Engine!B6": "expenditure_growth",
    }


def test_build_address_to_series_id_falls_back_to_public_output_series() -> None:
    internal_series = [
        {
            "id": "revenue_growth",
            "cells": [{"address": "Engine!B5", "key": {}}],
        },
    ]
    output_series = [
        {
            "id": "scenario_gdp_growth_hot",
            "compute_name": "compute_scenario_gdp_growth",
            "cells": [
                {"address": "Hot!D11", "key": {"TIME_PERIOD": 2010}},
                {"address": "Hot!E11", "key": {"TIME_PERIOD": 2011}},
            ],
        },
    ]

    assert build_address_to_series_id(
        internal_series,
        output_series=output_series,
    ) == {
        "Engine!B5": "revenue_growth",
        "Hot!D11": "scenario_gdp_growth_hot",
        "Hot!E11": "scenario_gdp_growth_hot",
    }


def test_build_address_to_series_id_prefers_internal_over_output_on_dual_bound() -> (
    None
):
    internal_series = [
        {
            "id": "internal_gdp",
            "cells": [{"address": "Outputs!B14", "key": {"TIME_PERIOD": 1}}],
        },
    ]
    output_series = [
        {
            "id": "public_gdp",
            "compute_name": "compute_gdp",
            "cells": [{"address": "Outputs!B14", "key": {"TIME_PERIOD": 1}}],
        },
    ]

    assert build_address_to_series_id(
        internal_series,
        output_series=output_series,
    ) == {"Outputs!B14": "internal_gdp"}


def test_build_address_to_series_id_falls_back_to_input_series() -> None:
    input_series = [
        {
            "id": "override_growth",
            "cells": [
                {"address": "Inputs!B9", "key": {"TIME_PERIOD": 1}},
                {"address": "Inputs!C9", "key": {"TIME_PERIOD": 2}},
            ],
        },
    ]

    assert build_address_to_series_id(
        (),
        input_series=input_series,
    ) == {
        "Inputs!B9": "override_growth",
        "Inputs!C9": "override_growth",
    }


def test_build_address_to_series_id_falls_back_to_constant_series() -> None:
    constant_series = [
        {
            "id": "demography_variant_label_medium",
            "cells": [{"address": "Demography!B8", "key": {}}],
        },
    ]

    assert build_address_to_series_id(
        (),
        constant_series=constant_series,
    ) == {"Demography!B8": "demography_variant_label_medium"}


def test_build_address_to_series_id_prefers_internal_over_constant() -> None:
    internal_series = [
        {
            "id": "internal_label",
            "cells": [{"address": "Demography!B8", "key": {}}],
        },
    ]
    constant_series = [
        {
            "id": "demography_variant_label_medium",
            "cells": [{"address": "Demography!B8", "key": {}}],
        },
    ]

    assert build_address_to_series_id(
        internal_series,
        constant_series=constant_series,
    ) == {"Demography!B8": "internal_label"}


def test_build_address_to_series_id_raises_on_duplicate_addresses() -> None:
    internal_series = [
        {
            "id": "series_a",
            "cells": [{"address": "Engine!B5", "key": {}}],
        },
        {
            "id": "series_b",
            "cells": [{"address": "Engine!B5", "key": {}}],
        },
    ]

    with pytest.raises(ValueError, match="exactly one series_id"):
        build_address_to_series_id(internal_series)


def test_build_address_to_series_id_raises_on_duplicate_public_addresses() -> None:
    output_series = [
        {
            "id": "series_a",
            "cells": [{"address": "Outputs!B1", "key": {}}],
        },
        {
            "id": "series_b",
            "cells": [{"address": "Outputs!B1", "key": {}}],
        },
    ]

    with pytest.raises(ValueError, match="exactly one series_id"):
        build_address_to_series_id((), output_series=output_series)
