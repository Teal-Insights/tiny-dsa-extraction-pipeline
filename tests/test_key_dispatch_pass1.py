"""Pass-1 wiring tests for key-dispatch multi-regime series."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from excel_grapher.exporter import ProjectionResult

from src.formula_clustering import FormulaCluster
from src.internals_refactor import (
    InternalsSourceIndex,
    _try_synthesize_cluster_body,
    build_cluster_refactor_context,
    build_mechanical_cluster_response,
    validate_cluster_refactor_response,
)
from src.refactor_bindings import BindingKeyValue, KeyConceptSpec

RUNTIME_IMPORT = """from __future__ import annotations

from .runtime import (
    EvalContext,
    xl_cell,
    xl_number,
)
"""

DSPB_LIKE_CLUSTER = FormulaCluster(
    cluster_id=90,
    members=(
        "Engine!B10",
        "Engine!C10",
        "Engine!D10",
        "Engine!E10",
        "Engine!F10",
        "Engine!G10",
    ),
    canonical_template="=Inputs!B1",
    row=10,
)

DSPB_LIKE_FORMULAS = {
    "Engine!B10": "=Inputs!B1",
    "Engine!C10": "=Paris!AS36",
    "Engine!D10": "=Engine!C10-Engine!B10",
    "Engine!E10": "=Inputs!C1",
    "Engine!F10": "=Paris!BR36",
    "Engine!G10": "=Engine!F10-Engine!E10",
}

DSPB_LIKE_MEMBER_KEYS: dict[str, dict[str, BindingKeyValue]] = {
    "Engine!B10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB"},
    "Engine!C10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB*"},
    "Engine!D10": {"TIME_PERIOD": 2050, "FISCAL_MEASURE": "PB Gap"},
    "Engine!E10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB"},
    "Engine!F10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB*"},
    "Engine!G10": {"TIME_PERIOD": 2075, "FISCAL_MEASURE": "PB Gap"},
}

# Operand addresses referenced by the leaf regimes (needed for fingerprinting).
DSPB_LIKE_OPERAND_KEYS: dict[str, dict[str, BindingKeyValue]] = {
    "Inputs!B1": {"TIME_PERIOD": 2050},
    "Inputs!C1": {"TIME_PERIOD": 2075},
    "Paris!AS36": {"TIME_PERIOD": 2050},
    "Paris!BR36": {"TIME_PERIOD": 2075},
}

DSPB_LIKE_SERIES_MAP = {
    address: "dspb_milestones" for address in DSPB_LIKE_CLUSTER.members
}

DSPB_LIKE_VOCABULARY = (
    KeyConceptSpec(
        dimension_id="TIME_PERIOD",
        concept="TIME_PERIOD",
        dtype="int",
        suggested_param_name="time_period",
    ),
    KeyConceptSpec(
        dimension_id="FISCAL_MEASURE",
        concept="FISCAL_MEASURE",
        dtype="str",
        suggested_param_name="fiscal_measure",
    ),
)

DSPB_LIKE_INTERNALS = (
    RUNTIME_IMPORT
    + """
# --- Formula cell functions ---

def cell_engine_b10(ctx):
    return xl_number(xl_cell(ctx, 'Inputs!B1'))

def cell_engine_c10(ctx):
    return xl_number(xl_cell(ctx, 'Paris!AS36'))

def cell_engine_d10(ctx):
    return xl_number(xl_cell(ctx, 'Paris!AS36')) - xl_number(xl_cell(ctx, 'Inputs!B1'))

def cell_engine_e10(ctx):
    return xl_number(xl_cell(ctx, 'Inputs!C1'))

def cell_engine_f10(ctx):
    return xl_number(xl_cell(ctx, 'Paris!BR36'))

def cell_engine_g10(ctx):
    return xl_number(xl_cell(ctx, 'Paris!BR36')) - xl_number(xl_cell(ctx, 'Inputs!C1'))

# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {}
_SYMBOL_DISPATCH = {}

def _address_to_func_name(address):
    return "cell_placeholder"

def _resolve_formula(address):
    return globals().get(_address_to_func_name(address))
"""
)


class _ProjectionStub:
    def __init__(
        self,
        formulas: dict[str, str],
        dependencies: dict[str, tuple[str, ...]],
    ) -> None:
        self._formulas = formulas
        self._dependencies = dependencies

    def __iter__(self) -> Iterator[str]:
        return iter(self._formulas)

    def get_node(self, address: str) -> object | None:
        formula = self._formulas.get(address)
        if formula is None:
            return None
        return type("Node", (), {"is_leaf": False, "normalized_formula": formula})()

    def get_dependencies(self, address: str) -> tuple[str, ...]:
        return self._dependencies.get(address, ())


def _dspb_projection() -> ProjectionResult:
    dependencies = {
        "Engine!B10": ("Inputs!B1",),
        "Engine!C10": ("Paris!AS36",),
        "Engine!D10": ("Engine!C10", "Engine!B10"),
        "Engine!E10": ("Inputs!C1",),
        "Engine!F10": ("Paris!BR36",),
        "Engine!G10": ("Engine!F10", "Engine!E10"),
    }
    return cast(ProjectionResult, _ProjectionStub(DSPB_LIKE_FORMULAS, dependencies))


def _write_dspb_internals(tmp_path: Path) -> Path:
    path = tmp_path / "internals.py"
    path.write_text(DSPB_LIKE_INTERNALS, encoding="utf-8")
    (tmp_path / "runtime.py").write_text(
        "def xl_cell(ctx, address):\n    return 0\n"
        "def xl_number(value):\n    return value\n",
        encoding="utf-8",
    )
    return path


def test_build_cluster_refactor_context_rescues_multi_regime_as_key_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: frozenset({"xl_cell", "xl_number", "ctx"}),
    )
    bound_keys = {**DSPB_LIKE_MEMBER_KEYS, **DSPB_LIKE_OPERAND_KEYS}
    ctx = build_cluster_refactor_context(
        _dspb_projection(),
        DSPB_LIKE_CLUSTER,
        _write_dspb_internals(tmp_path),
        bound_address_keys=bound_keys,
        key_vocabulary=DSPB_LIKE_VOCABULARY,
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        address_to_series_id=DSPB_LIKE_SERIES_MAP,
    )
    assert ctx is not None
    assert ctx.contract == "key_dispatch"
    assert ctx.key_dispatch_plan is not None
    assert ctx.key_dispatch_plan.dispatch_dimension_id == "FISCAL_MEASURE"
    assert len(ctx.key_dispatch_plan.regimes) == 3


def test_try_synthesize_cluster_body_emits_key_dispatch_control_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: frozenset({"xl_cell", "xl_number", "ctx"}),
    )
    bound_keys = {**DSPB_LIKE_MEMBER_KEYS, **DSPB_LIKE_OPERAND_KEYS}
    ctx = build_cluster_refactor_context(
        _dspb_projection(),
        DSPB_LIKE_CLUSTER,
        _write_dspb_internals(tmp_path),
        bound_address_keys=bound_keys,
        key_vocabulary=DSPB_LIKE_VOCABULARY,
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        address_to_series_id=DSPB_LIKE_SERIES_MAP,
    )
    assert ctx is not None
    draft = _try_synthesize_cluster_body(ctx)
    assert draft is not None
    assert "fiscal_measure" in draft.body
    assert "if fiscal_measure == 'PB':" in draft.body
    assert "if fiscal_measure == 'PB*':" in draft.body
    assert "if fiscal_measure == 'PB Gap':" in draft.body
    assert "ctx" in draft.body
    assert "-" in draft.body


def test_key_dispatch_mechanical_response_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.internals_refactor.allowed_runtime_symbols",
        lambda: frozenset({"xl_cell", "xl_number", "ctx", "xl_memoize"}),
    )
    internals_path = _write_dspb_internals(tmp_path)
    bound_keys = {**DSPB_LIKE_MEMBER_KEYS, **DSPB_LIKE_OPERAND_KEYS}
    ctx = build_cluster_refactor_context(
        _dspb_projection(),
        DSPB_LIKE_CLUSTER,
        internals_path,
        bound_address_keys=bound_keys,
        key_vocabulary=DSPB_LIKE_VOCABULARY,
        workbook_path=tmp_path / "workbook.xlsx",
        bindings_path=tmp_path / "bindings",
        address_to_series_id=DSPB_LIKE_SERIES_MAP,
    )
    assert ctx is not None
    draft = _try_synthesize_cluster_body(ctx)
    assert draft is not None
    internals_source = internals_path.read_text(encoding="utf-8")
    runtime_source = (tmp_path / "runtime.py").read_text(encoding="utf-8")
    response = build_mechanical_cluster_response(
        ctx,
        draft,
        runtime_source=runtime_source,
        internals_source=internals_source,
    )
    assert "fiscal_measure" in response.helper_source
    assert "@xl_memoize" in response.helper_source
    existing = frozenset(InternalsSourceIndex.from_source(internals_source).functions)
    validate_cluster_refactor_response(
        ctx,
        response,
        existing_names=existing,
        internals_source=internals_source,
        require_semantic_locals=False,
    )


def test_cluster_contract_bucket_reports_key_dispatch_for_multi_regime(
    tmp_path: Path,
) -> None:
    from src.record_refactor_buckets import _cluster_contract_and_skip_reason

    graph = _dspb_projection()
    contract, skip_reason = _cluster_contract_and_skip_reason(
        graph,  # type: ignore[arg-type]
        DSPB_LIKE_CLUSTER,
        DSPB_LIKE_INTERNALS,
        layout=None,
        bound_address_keys={**DSPB_LIKE_MEMBER_KEYS, **DSPB_LIKE_OPERAND_KEYS},
        key_vocabulary=DSPB_LIKE_VOCABULARY,
        workbook_path=tmp_path / "workbook.xlsx",
    )
    assert skip_reason is None
    assert contract == "key_dispatch"
