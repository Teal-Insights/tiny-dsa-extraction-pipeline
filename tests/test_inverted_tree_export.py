"""Layer B canary: inverted-tree export on the pipeline graph with CONSTRAINTS."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

import pytest
from excel_grapher.evaluator import FormulaEvaluator

from src.inverted_tree_export import (
    generate_inverted_tree_modules,
    inverted_tree_package_root,
    write_inverted_tree_package,
)
from src.pipeline_config import PipelineConfig

_DEFAULT_BASELINE = (
    61.28985507246378,
    62.0859413288525,
    62.38587341256677,
    62.18725444354536,
    61.48767596259631,
)
_DEFAULT_SHOCKED = (
    61.28985507246378,
    63.29945741415009,
    64.85855735045921,
    65.95605876303210,
    66.58059223010186,
)

_FORMULA_HELPERS = (
    "initial_debt_resolved",
    "shock_active",
    "shocked_growth",
    "baseline_path_internal",
    "output_baseline",
    "shocked_path_internal",
    "output_shocked",
    "output_delta",
    "shock_magnitude_resolved",
)


def _required(function: Callable[..., object]) -> tuple[str, ...]:
    return tuple(
        name
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.default is inspect.Parameter.empty
    )


def _all_params(function: Callable[..., object]) -> tuple[str, ...]:
    return tuple(inspect.signature(function).parameters)


def _assert_keyword_only(function: Callable[..., object]) -> None:
    for parameter in inspect.signature(function).parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def _load_package(
    modules: Mapping[str, str], tmp_path: Path, *, name: str
) -> ModuleType:
    package_dir = tmp_path / name
    package_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in modules.items():
        (package_dir / filename).write_text(content, encoding="utf-8")
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    for key in list(sys.modules):
        if key == name or key.startswith(f"{name}."):
            del sys.modules[key]
    package = __import__(name)
    for sub in ("api", "internals", "runtime", "data"):
        __import__(f"{name}.{sub}")
    return package


@pytest.fixture(scope="module")
def inverted_tree_pkg(
    tiny_dsa_configured_pipeline, tmp_path_factory: pytest.TempPathFactory
) -> ModuleType:
    pipeline = tiny_dsa_configured_pipeline
    before = getattr(pipeline.graph, "leaf_classification", None)
    modules = generate_inverted_tree_modules(
        pipeline.config,
        graph=pipeline.graph,
        series_bindings=pipeline.series_bindings,
    )
    assert getattr(pipeline.graph, "leaf_classification", None) == before
    tmp_path = tmp_path_factory.mktemp("tiny_dsa_inverted")
    return _load_package(modules, tmp_path, name="tiny_dsa_inverted")


def test_inverted_tree_package_root_is_sibling_of_ctx_package(
    tiny_dsa_configured_pipeline,
) -> None:
    config: PipelineConfig = tiny_dsa_configured_pipeline.config
    root = inverted_tree_package_root(config)
    assert root == config.dist_root / "tiny_dsa_inverted"
    assert root != config.package_root


def test_write_inverted_tree_package_does_not_clobber_ctx_package(
    tiny_dsa_configured_pipeline, tmp_path: Path
) -> None:
    from dataclasses import replace

    config = replace(tiny_dsa_configured_pipeline.config, dist_root=tmp_path / "dist")
    ctx_root = config.package_root
    ctx_root.mkdir(parents=True)
    (ctx_root / "api.py").write_text("CTX = True\n", encoding="utf-8")
    written = write_inverted_tree_package(
        config, {"api.py": "INVERTED = True\n", "runtime.py": "pass\n"}
    )
    assert written == tmp_path / "dist" / "tiny_dsa_inverted"
    assert (written / "api.py").read_text(encoding="utf-8") == "INVERTED = True\n"
    assert (ctx_root / "api.py").read_text(encoding="utf-8") == "CTX = True\n"


def test_helper_inventory_matches_bound_formula_series(inverted_tree_pkg) -> None:
    internals = inverted_tree_pkg.internals
    for name in _FORMULA_HELPERS:
        assert callable(getattr(internals, name))
    internals_source = inspect.getsource(internals)
    api_source = inspect.getsource(inverted_tree_pkg.api)
    assert "def cell_" not in internals_source
    assert "def make_context" not in api_source
    assert "def set_" not in api_source


def test_baseline_leaf_closure_excludes_shock_args(inverted_tree_pkg) -> None:
    compute = inverted_tree_pkg.compute_output_baseline
    _assert_keyword_only(compute)
    required = _required(compute)
    names = _all_params(compute)
    assert required == (
        "country_name",
        "country_initial_debt",
        "growth_baseline",
        "interest_baseline",
        "primary_balance_baseline",
    )
    assert "shock_year" not in names
    assert "shock_type" not in names
    assert "shock_magnitudes" not in names
    assert "engine_year_labels" not in names
    assert "ctx" not in names
    assert "country_profile_names" in names


def test_shocked_leaf_closure_includes_shock_args(inverted_tree_pkg) -> None:
    compute = inverted_tree_pkg.compute_output_shocked
    _assert_keyword_only(compute)
    _assert_keyword_only(inverted_tree_pkg.compute_output_delta)
    required = _required(compute)
    names = _all_params(compute)
    assert required == (
        "country_name",
        "country_initial_debt",
        "growth_baseline",
        "interest_baseline",
        "primary_balance_baseline",
        "shock_year",
        "shock_type",
        "shock_magnitudes",
    )
    assert "engine_year_labels" in names
    assert "country_profile_names" in names
    assert "ctx" not in names
    assert _required(inverted_tree_pkg.compute_output_delta) == required


def test_shocked_path_internal_first_level_deps(inverted_tree_pkg) -> None:
    names = _all_params(inverted_tree_pkg.internals.shocked_path_internal)
    assert "shocked_growth" in names
    assert "shocked_interest" in names
    assert "shocked_primary_balance" in names
    assert "shock_type" not in names
    assert "shock_magnitudes" not in names
    assert "ctx" not in names
    assert "engine_initial_debt_shocked" in names


def test_shock_active_params(inverted_tree_pkg) -> None:
    assert _required(inverted_tree_pkg.internals.shock_active) == (
        "engine_year_labels",
        "shock_year",
    )


def test_default_borvelia_numeric_parity(inverted_tree_pkg) -> None:
    data = inverted_tree_pkg.data
    baseline = inverted_tree_pkg.compute_output_baseline(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
        growth_baseline=data.GROWTH_BASELINE_DEFAULT,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
    )
    shocked = inverted_tree_pkg.compute_output_shocked(
        country_name=data.COUNTRY_NAME_DEFAULT,
        country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
        growth_baseline=data.GROWTH_BASELINE_DEFAULT,
        interest_baseline=data.INTEREST_BASELINE_DEFAULT,
        primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT,
        shock_year=data.SHOCK_YEAR_DEFAULT,
        shock_type=data.SHOCK_TYPE_DEFAULT,
        shock_magnitudes=data.SHOCK_MAGNITUDES_DEFAULT,
    )
    assert baseline == pytest.approx(_DEFAULT_BASELINE, abs=1e-9)
    assert shocked == pytest.approx(_DEFAULT_SHOCKED, abs=1e-9)


def test_pipeline_graph_formula_evaluator_parity(
    tiny_dsa_configured_pipeline,
) -> None:
    evaluator = FormulaEvaluator(tiny_dsa_configured_pipeline.graph)
    outputs = evaluator.evaluate(
        [f"Outputs!{col}12" for col in "BCDEF"]
        + [f"Outputs!{col}13" for col in "BCDEF"]
    )
    baseline = tuple(outputs[f"Outputs!{col}12"] for col in "BCDEF")
    shocked = tuple(outputs[f"Outputs!{col}13"] for col in "BCDEF")
    assert baseline == pytest.approx(_DEFAULT_BASELINE, abs=1e-9)
    assert shocked == pytest.approx(_DEFAULT_SHOCKED, abs=1e-9)


def test_compute_requires_catalog_order_full_length(inverted_tree_pkg) -> None:
    """Public compute_* takes catalog-order arrays; prefixes fail closed."""
    data = inverted_tree_pkg.data
    source = inspect.getsource(inverted_tree_pkg.compute_output_shocked)
    assert "require_length(growth_baseline, 5)" in source
    assert "require_length(engine_year_labels, 5)" in source
    with pytest.raises(ValueError, match="expected length 5, got 1"):
        inverted_tree_pkg.compute_output_shocked(
            country_name=data.COUNTRY_NAME_DEFAULT,
            country_initial_debt=data.COUNTRY_INITIAL_DEBT_DEFAULT,
            growth_baseline=data.GROWTH_BASELINE_DEFAULT[:1],
            interest_baseline=data.INTEREST_BASELINE_DEFAULT[:1],
            primary_balance_baseline=data.PRIMARY_BALANCE_BASELINE_DEFAULT[:1],
            shock_year=data.SHOCK_YEAR_DEFAULT,
            shock_type=data.SHOCK_TYPE_DEFAULT,
            shock_magnitudes=data.SHOCK_MAGNITUDES_DEFAULT,
        )
