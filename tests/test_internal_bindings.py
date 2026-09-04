"""Tests for internal series binding derivation and indexing."""

from __future__ import annotations

import pytest

from src.extraction_pipeline import build_pipeline_graph
from src.internal_bindings import (
    build_address_to_series_id,
    build_bound_address_keys,
    build_internal_binding_index,
    internal_series_cell_keys,
)
from tests.conftest import SyntheticConfiguredPipeline


def test_build_pipeline_graph_derives_internal_series(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    graph_result = build_pipeline_graph(synthetic_configured_pipeline.config)
    assert graph_result.internal_series
    internal_addresses = internal_series_cell_keys(graph_result.internal_series)
    assert internal_addresses == {"Engine!B2", "Engine!C2"}


def test_internal_binding_index_maps_formula_addresses(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    index = build_internal_binding_index(synthetic_configured_pipeline.internal_series)
    assert set(index) == {"Engine!B2", "Engine!C2"}
    assert index["Engine!B2"]["address"] == "Engine!B2"
    assert "key" in index["Engine!B2"]
    assert "record" in index["Engine!B2"]


def test_graph_nodes_do_not_carry_semantic_label_metadata(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    for address in ("Engine!B2", "Engine!C2"):
        node = synthetic_configured_pipeline.graph.get_node(address)
        assert node is not None
        metadata = node.metadata or {}
        assert "table_labels" not in metadata
        assert "row_labels" not in metadata
        assert "column_labels" not in metadata


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
