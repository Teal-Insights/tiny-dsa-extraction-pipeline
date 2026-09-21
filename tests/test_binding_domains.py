from __future__ import annotations

from typing import Annotated, Literal, get_args, get_origin

from excel_grapher.core.cell_types import Between, RealBetween, normalize_cell_type_env_key
from excel_grapher.series_bindings import load_series_bindings
from excel_grapher.series_bindings.domains import cell_type_env_from_bindings, undomained_leaves

from src.binding_domains import (
    domain_annotations_from_bindings,
    effective_domain_annotations,
    pipeline_dynamic_ref_config,
)
from src.extraction_pipeline import classify_leaves_from_constraints, is_constant_constraint
from src.pipeline_config import load_pipeline_config

_COLS = ("C", "D", "E", "F", "G")
_FORMER_CONSTRAINT_KEYS = frozenset(
    {
        "Inputs!A10",
        "Inputs!A11",
        "Inputs!A12",
        "Inputs!B22",
        "Inputs!B5",
        "Engine!C5",
        "Engine!D5",
        "Engine!E5",
        "Engine!F5",
        "Engine!G5",
        "Inputs!B10",
        "Inputs!B11",
        "Inputs!B12",
        "Inputs!B21",
        "Inputs!B26",
        "Inputs!C26",
        "Inputs!D26",
        *(f"Inputs!{col}16" for col in _COLS),
        *(f"Inputs!{col}17" for col in _COLS),
        *(f"Inputs!{col}18" for col in _COLS),
    }
)


def _normalized(keys: frozenset[str]) -> set[str]:
    return {normalize_cell_type_env_key(key) for key in keys}


def test_tiny_dsa_sidecar_compiles_the_former_constraint_keys() -> None:
    config = load_pipeline_config()
    bindings = load_series_bindings(config.bindings_path)
    env = cell_type_env_from_bindings(bindings, workbook=config.workbook_path)
    assert _normalized(frozenset(env)) == _normalized(_FORMER_CONSTRAINT_KEYS)
    assert config.constraints == {}


def test_tiny_dsa_from_bindings_covers_former_constraint_keys() -> None:
    config = load_pipeline_config()
    derived = pipeline_dynamic_ref_config(config)
    assert _normalized(frozenset(derived.cell_type_env)) == _normalized(
        _FORMER_CONSTRAINT_KEYS
    )


def test_tiny_dsa_domain_annotations_match_constraint_kinds() -> None:
    config = load_pipeline_config()
    bindings = load_series_bindings(config.bindings_path)
    annotations = domain_annotations_from_bindings(
        bindings, workbook=config.workbook_path
    )
    assert _normalized(frozenset(annotations)) == _normalized(_FORMER_CONSTRAINT_KEYS)

    country_name = annotations["Inputs!B5"]
    assert get_origin(country_name) is Literal
    assert set(get_args(country_name)) == {"Borvelia", "Litellia", "Aurelium"}

    shock_year = annotations["Inputs!B21"]
    assert get_origin(shock_year) is Annotated
    assert Between(1, 5) in get_args(shock_year)

    debt = annotations["Inputs!B10"]
    assert get_origin(debt) is Annotated
    assert RealBetween(0.0, 200.0) in get_args(debt)

    assert is_constant_constraint(annotations["Inputs!A10"])
    assert is_constant_constraint(annotations["Engine!C5"])
    assert not is_constant_constraint(annotations["Inputs!B5"])


def test_effective_domain_annotations_overlay_wins(
    synthetic_pipeline_config_fixture,
) -> None:
    overlay = {"Inputs!A1": Annotated[float, RealBetween(1.0, 2.0)]}
    from dataclasses import replace

    config = replace(synthetic_pipeline_config_fixture, constraints=overlay)
    annotations = effective_domain_annotations(config)
    assert annotations["Inputs!A1"] is overlay["Inputs!A1"]


def test_tiny_dsa_leaf_classification_from_sidecar(
    tiny_dsa_configured_pipeline,
) -> None:
    pipeline = tiny_dsa_configured_pipeline
    expected = classify_leaves_from_constraints(
        effective_domain_annotations(pipeline.config),
        pipeline.graph.leaf_keys(),
    )
    assert pipeline.leaf_classification == expected
    missing = undomained_leaves(
        pipeline.graph,
        pipeline.series_bindings,
        workbook=pipeline.config.workbook_path,
    )
    assert missing == []
