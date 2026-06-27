"""RED-first tests for the in-loop behavioral parity gate.

The gate runs after a cluster (or singleton) is refactored: it evaluates the
refactored helper against the pristine pre-refactor cell semantics across several
input vectors, and raises ``ParityError`` (rolling back the transaction and
triggering an LLM re-prompt) when the values diverge.
"""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from excel_grapher.core.cell_types import Between, RealBetween

from src.internals_refactor import (
    ALLOWED_RUNTIME_SYMBOLS,
    ClusterRefactorResponse,
    HelperParameter,
    MemberKeys,
    SingletonRefactorContext,
    SingletonRefactorResponse,
)
from src.refactor_parity_gate import (
    ParityError,
    check_cluster_parity,
    check_singleton_parity,
    sample_input_vectors,
)

RUNTIME_IMPORT = """from __future__ import annotations

from .runtime import (
    XlError,
    np,
    to_bool,
    to_int,
    xl_add,
    xl_cell,
    xl_div,
    xl_eval,
    xl_ge,
    xl_index_ref,
    xl_match,
    xl_mul,
    xl_offset,
    xl_sub,
)
"""

RESOLVER_SECTION = """# --- Formula resolver ---
_RESOLVED_FORMULAS = {}
_ADDRESS_DISPATCH = {}
_SYMBOL_DISPATCH = {}

def _address_to_func_name(address):
    name = []
    prev_underscore = False
    for ch in address.lower():
        if ch == "'":
            continue
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            name.append(ch)
            prev_underscore = False
        else:
            if not prev_underscore:
                name.append("_")
                prev_underscore = True
    base = "".join(name).strip("_")
    return f"cell_{base}"

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    symbol_name = _SYMBOL_DISPATCH.get(address)
    if symbol_name is not None:
        helper = globals()[symbol_name]

        def _bound(ctx, _helper=helper):
            return _helper(ctx)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
    name = _address_to_func_name(address)
    fn = globals().get(name)
    if fn is not None:
        _RESOLVED_FORMULAS[address] = fn
    return fn
"""

PRISTINE_CLUSTER = (
    RUNTIME_IMPORT
    + '''
# --- Formula cell functions ---

def cell_engine_c6(ctx):
    """Covers Engine!C6."""
    return xl_add(xl_cell(ctx, 'Inputs!C1'), 0.0)

def cell_engine_d6(ctx):
    """Covers Engine!D6."""
    return xl_add(xl_cell(ctx, 'Inputs!D1'), 0.0)

'''
    + RESOLVER_SECTION
)

PRISTINE_SINGLETON = (
    RUNTIME_IMPORT
    + '''
# --- Formula cell functions ---

def cell_inputs_b6(ctx):
    """Covers Inputs!B6."""
    return xl_mul(xl_cell(ctx, 'Inputs!B2'), 2.0)

'''
    + RESOLVER_SECTION
)

CLUSTER_PARAMETERS = (
    HelperParameter(name="time_period", concept="TIME_PERIOD", dtype="int"),
)
CLUSTER_MEMBER_KEYS = (
    MemberKeys(
        address="Engine!C6", function_name="cell_engine_c6", keys={"TIME_PERIOD": 1}
    ),
    MemberKeys(
        address="Engine!D6", function_name="cell_engine_d6", keys={"TIME_PERIOD": 2}
    ),
)

CLUSTER_DOCSTRING = (
    "Return the passthrough input for a projection period.\n\n"
    "Args:\n    ctx: Workbook evaluation context.\n"
    "    time_period: Projection period.\n\n"
    "Returns:\n    The corresponding input value.\n"
)

CORRECT_CLUSTER_SOURCE = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    columns = {{1: 'C', 2: 'D'}}
    column = columns[time_period]
    return xl_add(xl_cell(ctx, f'Inputs!{{column}}1'), 0.0)
'''

# Broken: ignores time_period and always reads the year-1 column.
BROKEN_CLUSTER_SOURCE = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    return xl_add(xl_cell(ctx, 'Inputs!C1'), 0.0)
'''


def _cluster_response(helper_source: str) -> ClusterRefactorResponse:
    return ClusterRefactorResponse(
        helper_name="combined_input_passthrough",
        helper_docstring=CLUSTER_DOCSTRING,
        uses_first_year_branch=False,
        parameters=CLUSTER_PARAMETERS,
        helper_source=helper_source,
        member_keys=CLUSTER_MEMBER_KEYS,
    )


CLUSTER_INPUTS = [
    {"Inputs!C1": 10.0, "Inputs!D1": 20.0},
    {"Inputs!C1": 5.0, "Inputs!D1": 99.0},
]


def test_cluster_gate_passes_for_semantics_preserving_refactor() -> None:
    check_cluster_parity(
        pristine_source=PRISTINE_CLUSTER,
        current_source=PRISTINE_CLUSTER,
        response=_cluster_response(CORRECT_CLUSTER_SOURCE),
        input_vectors=CLUSTER_INPUTS,
    )


def test_cluster_gate_rejects_semantics_breaking_refactor() -> None:
    with pytest.raises(ParityError) as excinfo:
        check_cluster_parity(
            pristine_source=PRISTINE_CLUSTER,
            current_source=PRISTINE_CLUSTER,
            response=_cluster_response(BROKEN_CLUSTER_SOURCE),
            input_vectors=CLUSTER_INPUTS,
        )
    message = str(excinfo.value)
    assert "combined_input_passthrough" in message
    assert "Engine!D6" in message
    # Reports the diverging values so the model gets actionable feedback.
    assert "20.0" in message


SINGLETON_DOCSTRING = (
    "Return the initial value.\n\n"
    "Args:\n    ctx: Workbook evaluation context.\n\n"
    "Returns:\n    The initial value.\n"
)

CORRECT_SINGLETON_SOURCE = f'''def initial_value(ctx):
    """{SINGLETON_DOCSTRING}"""
    return xl_mul(xl_cell(ctx, 'Inputs!B2'), 2.0)
'''

BROKEN_SINGLETON_SOURCE = f'''def initial_value(ctx):
    """{SINGLETON_DOCSTRING}"""
    return xl_mul(xl_cell(ctx, 'Inputs!B2'), 3.0)
'''


def _singleton_context() -> SingletonRefactorContext:
    return SingletonRefactorContext(
        address="Inputs!B6",
        function_name="cell_inputs_b6",
        canonical_template="",
        normalized_formula="",
        python_source="",
        dependency_addresses=(),
        external_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
    )


def _singleton_response(symbol_source: str) -> SingletonRefactorResponse:
    return SingletonRefactorResponse(
        symbol_name="initial_value",
        symbol_docstring=SINGLETON_DOCSTRING,
        symbol_source=symbol_source,
    )


SINGLETON_INPUTS = [{"Inputs!B2": 7.0}, {"Inputs!B2": 11.0}]


def test_singleton_gate_passes_for_semantics_preserving_refactor() -> None:
    check_singleton_parity(
        pristine_source=PRISTINE_SINGLETON,
        current_source=PRISTINE_SINGLETON,
        response=_singleton_response(CORRECT_SINGLETON_SOURCE),
        ctx=_singleton_context(),
        input_vectors=SINGLETON_INPUTS,
    )


def test_singleton_gate_rejects_semantics_breaking_refactor() -> None:
    with pytest.raises(ParityError) as excinfo:
        check_singleton_parity(
            pristine_source=PRISTINE_SINGLETON,
            current_source=PRISTINE_SINGLETON,
            response=_singleton_response(BROKEN_SINGLETON_SOURCE),
            ctx=_singleton_context(),
            input_vectors=SINGLETON_INPUTS,
        )
    assert "Inputs!B6" in str(excinfo.value)


def test_sample_input_vectors_respects_domains_and_is_deterministic() -> None:
    constraints = {
        "Inputs!B5": Literal["Borvelia", "Litellia", "Aurelium"],
        "Inputs!B21": Annotated[int, Between(1, 5)],
        "Inputs!C16": Annotated[float, RealBetween(-10.0, 15.0)],
        "Inputs!A10": Literal["Borvelia"],
        "Engine!C5": Literal[1],
    }
    default_inputs = {"Inputs!B5": "Borvelia", "Inputs!B21": 2, "Inputs!C16": 3.5}
    constants = {"Inputs!A10": "Borvelia", "Engine!C5": 1}

    vectors = sample_input_vectors(
        constraints=constraints,
        default_inputs=default_inputs,
        constants=constants,
        count=5,
        seed=0,
    )

    # First vector is the canonical default merged with constants.
    assert vectors[0] == {**default_inputs, **constants}
    assert len(vectors) == 6

    for vector in vectors:
        assert vector["Inputs!B5"] in {"Borvelia", "Litellia", "Aurelium"}
        shock_year = vector["Inputs!B21"]
        assert isinstance(shock_year, int)
        assert 1 <= shock_year <= 5
        growth_rate = vector["Inputs!C16"]
        assert isinstance(growth_rate, float)
        assert -10.0 <= growth_rate <= 15.0
        # Constants are never perturbed.
        assert vector["Inputs!A10"] == "Borvelia"
        assert vector["Engine!C5"] == 1

    # Deterministic under a fixed seed.
    again = sample_input_vectors(
        constraints=constraints,
        default_inputs=default_inputs,
        constants=constants,
        count=5,
        seed=0,
    )
    assert vectors == again
