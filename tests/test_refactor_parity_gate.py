"""RED-first tests for the in-loop behavioral parity gate.

The gate runs after a cluster (or singleton) is refactored: it evaluates the
refactored helper against the pristine pre-refactor cell semantics across several
input vectors, and raises ``ParityError`` (rolling back the transaction and
triggering an LLM re-prompt) when the values diverge.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal

import pytest
from excel_grapher.core.cell_types import Between, RealBetween

from src.internals_refactor import (
    ClusterRefactorResponse,
    HelperParameter,
    MemberKeyEntry,
    MemberKeys,
    SingletonRefactorContext,
    SingletonRefactorResponse,
)
from src.pipeline_config import load_pipeline_config
from src.pipeline_context import activate_pipeline_config
from src.refactor_parity_gate import (
    ParityError,
    _evaluate_candidate,
    _evaluate_golden,
    _load_candidate,
    check_cluster_parity,
    check_singleton_parity,
    exec_internals_module,
    sample_input_vectors,
)

RUNTIME_IMPORT = """from __future__ import annotations

from .runtime import (
    XlError,
    to_bool,
    to_int,
    xl_cell,
    xl_compare,
    xl_eval,
    xl_number,
    xl_offset,
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
    return xl_cell(ctx, 'Inputs!C1')

def cell_engine_d6(ctx):
    """Covers Engine!D6."""
    return xl_cell(ctx, 'Inputs!D1')

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

PRISTINE_XLERROR_SINGLETON = (
    RUNTIME_IMPORT
    + '''
# --- Formula cell functions ---

def cell_inputs_b6(ctx):
    """Covers Inputs!B6."""
    return XlError.VALUE

'''
    + RESOLVER_SECTION
)

CLUSTER_PARAMETERS = (
    HelperParameter(name="time_period", concept="TIME_PERIOD", dtype="int"),
)
CLUSTER_MEMBER_KEYS = (
    MemberKeys(
        address="Engine!C6",
        function_name="cell_engine_c6",
        keys=(MemberKeyEntry(concept="TIME_PERIOD", value=1),),
    ),
    MemberKeys(
        address="Engine!D6",
        function_name="cell_engine_d6",
        keys=(MemberKeyEntry(concept="TIME_PERIOD", value=2),),
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
    return xl_cell(ctx, f'Inputs!{{column}}1')
'''

BROKEN_CLUSTER_SOURCE = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    return xl_cell(ctx, 'Inputs!C1')
'''

CRASHING_CLUSTER_SOURCE = f'''def combined_input_passthrough(ctx, time_period):
    """{CLUSTER_DOCSTRING}"""
    return xl_add()
'''

MISSING_SYMBOL_CLUSTER_SOURCE = """def other_helper_name(ctx, time_period):
    return xl_cell(ctx, 'Inputs!C1')
"""


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


@pytest.fixture(scope="module")
def parity_gate_dist_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("parity_gate_dist")
    package_root = root / "tiny_dsa"
    package_root.mkdir(parents=True)
    fixture_runtime = (
        Path(__file__).resolve().parent / "fixtures" / "parity_gate_runtime.py"
    )
    shutil.copy(fixture_runtime, package_root / "runtime.py")
    (package_root / "data.py").write_text(
        "DEFAULT_INPUTS = {}\nCONSTANTS = {}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _parity_gate_active_config(parity_gate_dist_root: Path) -> Iterator[None]:
    config = replace(load_pipeline_config(), dist_root=parity_gate_dist_root)
    activate_pipeline_config(config)
    from tests.fixtures.test_state import clear_runtime_caches

    clear_runtime_caches()
    yield


def test_allowed_runtime_symbols_exist_on_fixture_runtime() -> None:
    from src.refactor_parity_gate import _runtime
    from src.runtime_symbols import allowed_runtime_symbols

    runtime = _runtime()
    for symbol in allowed_runtime_symbols():
        assert hasattr(runtime, symbol), symbol


def test_exec_pristine_cluster_source() -> None:
    namespace = exec_internals_module(PRISTINE_CLUSTER)
    assert callable(namespace["_resolve_formula"])
    assert callable(namespace["cell_engine_c6"])


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
    assert "20.0" in message


def test_cluster_gate_converts_runtime_error_to_parity_error() -> None:
    with pytest.raises(ParityError) as excinfo:
        check_cluster_parity(
            pristine_source=PRISTINE_CLUSTER,
            current_source=PRISTINE_CLUSTER,
            response=_cluster_response(CRASHING_CLUSTER_SOURCE),
            input_vectors=CLUSTER_INPUTS,
        )
    message = str(excinfo.value)
    assert "combined_input_passthrough" in message
    assert "TypeError" in message


def test_cluster_gate_surfaces_missing_symbol_as_retryable_parity_error() -> None:
    with pytest.raises(ParityError) as excinfo:
        check_cluster_parity(
            pristine_source=PRISTINE_CLUSTER,
            current_source=PRISTINE_CLUSTER,
            response=_cluster_response(MISSING_SYMBOL_CLUSTER_SOURCE),
            input_vectors=CLUSTER_INPUTS,
        )
    message = str(excinfo.value)
    assert "refactored symbol combined_input_passthrough could not be loaded" in message
    assert "KeyError" in message


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

CRASHING_SINGLETON_SOURCE = f'''def initial_value(ctx):
    """{SINGLETON_DOCSTRING}"""
    return xl_add()
'''

CORRECT_XLERROR_SINGLETON_SOURCE = f'''def initial_value(ctx):
    """{SINGLETON_DOCSTRING}"""
    return XlError.VALUE
'''


def _singleton_context() -> SingletonRefactorContext:
    from src.runtime_symbols import allowed_runtime_symbols

    return SingletonRefactorContext(
        address="Inputs!B6",
        function_name="cell_inputs_b6",
        canonical_template="",
        normalized_formula="",
        python_source="",
        dependency_addresses=(),
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=allowed_runtime_symbols(),
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


def test_singleton_gate_converts_runtime_error_to_parity_error() -> None:
    with pytest.raises(ParityError) as excinfo:
        check_singleton_parity(
            pristine_source=PRISTINE_SINGLETON,
            current_source=PRISTINE_SINGLETON,
            response=_singleton_response(CRASHING_SINGLETON_SOURCE),
            ctx=_singleton_context(),
            input_vectors=SINGLETON_INPUTS,
        )
    message = str(excinfo.value)
    assert "initial_value(ctx)" in message
    assert "TypeError" in message


def test_singleton_gate_passes_when_both_sides_return_excel_error() -> None:
    check_singleton_parity(
        pristine_source=PRISTINE_XLERROR_SINGLETON,
        current_source=PRISTINE_XLERROR_SINGLETON,
        response=_singleton_response(CORRECT_XLERROR_SINGLETON_SOURCE),
        ctx=_singleton_context(),
        input_vectors=SINGLETON_INPUTS,
    )


def test_evaluate_golden_normalizes_excel_errors() -> None:
    from src.refactor_parity_gate import _runtime

    runtime = _runtime()

    result = _evaluate_golden(
        lambda: (_ for _ in ()).throw(runtime.XlErrorException(runtime.XlError.VALUE))
    )
    assert result is runtime.XlError.VALUE


def test_evaluate_golden_propagates_non_excel_exceptions() -> None:
    with pytest.raises(RuntimeError, match="pipeline defect"):
        _evaluate_golden(lambda: (_ for _ in ()).throw(RuntimeError("pipeline defect")))


def test_evaluate_candidate_normalizes_excel_errors() -> None:
    from src.refactor_parity_gate import _runtime

    runtime = _runtime()

    result = _evaluate_candidate(
        lambda: (_ for _ in ()).throw(runtime.XlErrorException(runtime.XlError.NA)),
        call="helper(ctx)",
    )
    assert result is runtime.XlError.NA


def test_evaluate_candidate_wraps_non_excel_exceptions_with_call_site() -> None:
    with pytest.raises(ParityError) as excinfo:
        _evaluate_candidate(
            lambda: (_ for _ in ()).throw(ValueError("bad helper")),
            call="helper(ctx, time_period=1)",
        )
    message = str(excinfo.value)
    assert "helper(ctx, time_period=1)" in message
    assert "ValueError" in message


def test_load_candidate_surfaces_compile_errors_as_parity_error() -> None:
    with pytest.raises(ParityError) as excinfo:
        _load_candidate("def broken(\n", "broken")
    message = str(excinfo.value)
    assert "refactored symbol broken could not be loaded" in message
    assert "SyntaxError" in message


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
        assert vector["Inputs!A10"] == "Borvelia"
        assert vector["Engine!C5"] == 1

    again = sample_input_vectors(
        constraints=constraints,
        default_inputs=default_inputs,
        constants=constants,
        count=5,
        seed=0,
    )
    assert vectors == again
