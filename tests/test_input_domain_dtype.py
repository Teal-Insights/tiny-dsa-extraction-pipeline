"""Fail closed when input.domain kind does not match measure dtype."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from excel_grapher.series_bindings.types import WorkbookSeriesBindings

from src.extraction_pipeline import build_pipeline_graph
from src.input_domain_dtype import (
    input_domain_dtype_mismatches,
    require_input_domain_dtype_consistency,
)
from src.pipeline_config import PipelineConfig
from tests.conftest import SyntheticConfiguredPipeline


def _bindings(
    series: list[dict[str, Any]],
    *,
    concept_scheme: dict[str, Any] | None = None,
) -> WorkbookSeriesBindings:
    document: dict[str, Any] = {
        "schema_version": "1.13.0",
        "series": series,
    }
    if concept_scheme is not None:
        document["concept_scheme"] = concept_scheme
    return cast(WorkbookSeriesBindings, document)


def _input_series(
    series_id: str,
    *,
    dtype: str | None = "float",
    domain: Mapping[str, Any] | None = None,
    concept: str = "OBS_VALUE",
) -> dict[str, Any]:
    input_block: dict[str, Any] = {
        "setter": {
            "name": f"set_{series_id}",
            "record_contract": "records",
            "strict": True,
        }
    }
    if domain is not None:
        input_block["domain"] = dict(domain)
    measure: dict[str, Any] = {
        "concept": concept,
        "bind": {"kind": "data_cell"},
    }
    if dtype is not None:
        measure["dtype"] = dtype
        measure["bind"] = {"kind": "data_cell", "read": dtype}
    return {
        "id": series_id,
        "sheet": "Inputs",
        "data_range": "Inputs!A1",
        "layout": "scalar",
        "input": input_block,
        "structure": {"measure": measure, "dimensions": []},
        "key": [],
    }


def test_float_between_is_a_mismatch() -> None:
    bindings = _bindings(
        [
            _input_series(
                "share",
                dtype="float",
                domain={"between": {"min": 0, "max": 1}},
            )
        ]
    )
    assert input_domain_dtype_mismatches(bindings) == ("share",)
    with pytest.raises(ValueError, match=r"share"):
        require_input_domain_dtype_consistency(bindings)


def test_float_real_between_is_ok() -> None:
    bindings = _bindings(
        [
            _input_series(
                "share",
                dtype="float",
                domain={"real_between": {"min": 0, "max": 1}},
            )
        ]
    )
    require_input_domain_dtype_consistency(bindings)


def test_int_between_is_ok() -> None:
    bindings = _bindings(
        [
            _input_series(
                "years",
                dtype="int",
                domain={"between": {"min": 0, "max": 40}},
            )
        ]
    )
    require_input_domain_dtype_consistency(bindings)


def test_int_real_between_is_a_mismatch() -> None:
    bindings = _bindings(
        [
            _input_series(
                "years",
                dtype="int",
                domain={"real_between": {"min": 0, "max": 1}},
            )
        ]
    )
    assert input_domain_dtype_mismatches(bindings) == ("years",)
    with pytest.raises(ValueError, match=r"years"):
        require_input_domain_dtype_consistency(bindings)


def test_number_between_is_a_mismatch() -> None:
    bindings = _bindings(
        [
            _input_series(
                "rate",
                dtype="number",
                domain={"between": {"min": 0, "max": 1}},
            )
        ]
    )
    assert input_domain_dtype_mismatches(bindings) == ("rate",)


def test_number_real_between_is_ok() -> None:
    bindings = _bindings(
        [
            _input_series(
                "rate",
                dtype="number",
                domain={"real_between": {"min": 0.0, "max": 1.0}},
            )
        ]
    )
    require_input_domain_dtype_consistency(bindings)


def test_enum_is_ignored_for_numeric_dtype() -> None:
    bindings = _bindings(
        [
            _input_series(
                "flag",
                dtype="float",
                domain={"enum": [0, 1]},
            ),
            _input_series(
                "label",
                dtype="string",
                domain={"enum": ["High", "Low"]},
            ),
        ]
    )
    require_input_domain_dtype_consistency(bindings)


def test_missing_measure_dtype_with_numeric_domain_is_a_mismatch() -> None:
    bindings = _bindings(
        [
            _input_series(
                "share",
                dtype=None,
                domain={"between": {"min": 0, "max": 1}},
            )
        ]
    )
    assert input_domain_dtype_mismatches(bindings) == ("share",)


def test_concept_scheme_dtype_fallback_rejects_float_between() -> None:
    bindings = _bindings(
        [
            _input_series(
                "share",
                dtype=None,
                domain={"between": {"min": 0, "max": 1}},
            )
        ],
        concept_scheme={
            "id": "model",
            "concepts": [{"id": "OBS_VALUE", "name": "Observation", "dtype": "float"}],
        },
    )
    assert input_domain_dtype_mismatches(bindings) == ("share",)


def test_concept_scheme_dtype_fallback_accepts_int_between() -> None:
    bindings = _bindings(
        [
            _input_series(
                "years",
                dtype=None,
                domain={"between": {"min": 0, "max": 40}},
            )
        ],
        concept_scheme={
            "id": "model",
            "concepts": [{"id": "OBS_VALUE", "name": "Observation", "dtype": "int"}],
        },
    )
    require_input_domain_dtype_consistency(bindings)


def test_non_input_series_are_ignored() -> None:
    bindings = _bindings(
        [
            {
                "id": "result",
                "sheet": "Outputs",
                "data_range": "Outputs!B1",
                "layout": "scalar",
                "output": {"compute": {"name": "compute_result"}},
                "domain": {"between": {"min": 0, "max": 1}},
                "structure": {
                    "measure": {
                        "concept": "OBS_VALUE",
                        "dtype": "float",
                        "bind": {"kind": "data_cell", "read": "float"},
                    },
                    "dimensions": [],
                },
                "key": [],
            },
            {
                "id": "engine",
                "sheet": "Engine",
                "data_range": "Engine!B2",
                "layout": "scalar",
                "internal": {},
                "domain": {"between": {"min": 0, "max": 1}},
                "structure": {
                    "measure": {
                        "concept": "OBS_VALUE",
                        "dtype": "float",
                        "bind": {"kind": "data_cell", "read": "float"},
                    },
                    "dimensions": [],
                },
                "key": [],
            },
        ]
    )
    require_input_domain_dtype_consistency(bindings)


def test_require_lists_every_mismatched_series_id() -> None:
    bindings = _bindings(
        [
            _input_series(
                "share",
                dtype="float",
                domain={"between": {"min": 0, "max": 1}},
            ),
            _input_series(
                "overvaluation",
                dtype="float",
                domain={"between": {"min": 0, "max": 1}},
            ),
            _input_series(
                "discount_rate",
                dtype="float",
                domain={"real_between": {"min": 0, "max": 1}},
            ),
        ]
    )
    with pytest.raises(ValueError, match=r"share") as exc_info:
        require_input_domain_dtype_consistency(bindings)
    message = str(exc_info.value)
    assert "overvaluation" in message
    assert "discount_rate" not in message


def test_synthetic_input_domains_match_measure_dtype(
    synthetic_configured_pipeline: SyntheticConfiguredPipeline,
) -> None:
    require_input_domain_dtype_consistency(
        synthetic_configured_pipeline.series_bindings
    )


def test_build_pipeline_graph_rejects_float_between_domain(
    synthetic_pipeline_config_fixture: PipelineConfig,
    tmp_path: Path,
) -> None:
    bindings_path = tmp_path / "bindings"
    bindings_path.mkdir()
    for source in synthetic_pipeline_config_fixture.bindings_path.glob(
        "*.bindings.yaml"
    ):
        text = source.read_text(encoding="utf-8")
        if source.name == "inputs.bindings.yaml":
            document = yaml.safe_load(text)
            series = document["series"][0]
            series.setdefault("input", {})["domain"] = {"between": {"min": 0, "max": 1}}
            text = yaml.safe_dump(document, sort_keys=False)
        (bindings_path / source.name).write_text(text, encoding="utf-8")

    config = replace(synthetic_pipeline_config_fixture, bindings_path=bindings_path)
    with pytest.raises(ValueError, match=r"input_rate"):
        build_pipeline_graph(config)


def test_authoring_prompt_documents_between_vs_real_between() -> None:
    conventions = (
        Path(__file__).resolve().parents[1]
        / ".agents"
        / "skills"
        / "author-bindings"
        / "references"
        / "conventions.md"
    ).read_text(encoding="utf-8")
    assert "input.domain.between is an integer interval" in conventions
    assert "input.domain.real_between is a real interval" in conventions
    assert "dtype: float" in conventions or "dtype `float`" in conventions
    assert "Do not pair" in conventions and "between" in conventions
