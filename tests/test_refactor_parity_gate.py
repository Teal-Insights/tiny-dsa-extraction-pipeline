"""RED-first tests for the in-loop behavioral parity gate.

The gate runs after a cluster (or singleton) is refactored: it evaluates the
refactored helper against the pristine pre-refactor cell semantics across several
input vectors, and raises ``ParityError`` (rolling back the transaction and
triggering an LLM re-prompt) when the values diverge.
"""

from __future__ import annotations

import logging
import shutil
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
    HelperParameter(name="time_period", dimension_id="TIME_PERIOD", dtype="int"),
)
CLUSTER_MEMBER_KEYS = (
    MemberKeys(
        address="Engine!C6",
        function_name="cell_engine_c6",
        keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=1),),
    ),
    MemberKeys(
        address="Engine!D6",
        function_name="cell_engine_d6",
        keys=(MemberKeyEntry(dimension_id="TIME_PERIOD", value=2),),
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
    package_name = load_pipeline_config().dist_metadata.package_name
    package_root = root / package_name
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


@pytest.fixture
def package_root(parity_gate_dist_root: Path) -> Path:
    from tests.fixtures.test_state import clear_runtime_caches

    clear_runtime_caches()
    return parity_gate_dist_root / load_pipeline_config().dist_metadata.package_name


def test_allowed_runtime_symbols_exist_on_fixture_runtime(package_root: Path) -> None:
    from src.refactor_parity_gate import _runtime, _runtime_path
    from src.runtime_symbols import discover_allowed_runtime_symbols

    runtime = _runtime(str(package_root.resolve()))
    for symbol in discover_allowed_runtime_symbols(_runtime_path(package_root)):
        assert hasattr(runtime, symbol), symbol


def test_exec_internals_injects_reader_symbols(
    parity_gate_dist_root: Path, package_root: Path
) -> None:
    package_name = load_pipeline_config().dist_metadata.package_name
    readers_path = parity_gate_dist_root / package_name / "_readers.py"
    readers_path.write_text(
        "from .runtime import xl_cell\n\ndef read_shock_type(ctx):\n    return xl_cell(ctx, 'Inputs!B1')\n",
        encoding="utf-8",
    )
    from tests.fixtures.test_state import clear_runtime_caches

    clear_runtime_caches()
    source = (
        "from ._readers import read_shock_type\n"
        "from .runtime import xl_cell\n\n"
        "def _resolve_formula(ctx, address):\n"
        "    return None\n\n"
        "def cell_inputs_b1(ctx):\n"
        "    return read_shock_type(ctx)\n"
    )
    namespace = exec_internals_module(source, package_root=package_root)
    assert callable(namespace["read_shock_type"])
    assert callable(namespace["cell_inputs_b1"])


def test_exec_pristine_cluster_source(package_root: Path) -> None:
    namespace = exec_internals_module(PRISTINE_CLUSTER, package_root=package_root)
    assert callable(namespace["_resolve_formula"])
    assert callable(namespace["cell_engine_c6"])


def test_golden_namespace_caches_identical_pristine_source(package_root: Path) -> None:
    from src.refactor_parity_gate import _golden_namespace

    _golden_namespace.cache_clear()
    first = _golden_namespace(PRISTINE_CLUSTER, str(package_root.resolve()))
    second = _golden_namespace(PRISTINE_CLUSTER, str(package_root.resolve()))
    assert first is second
    assert callable(first["_resolve_formula"])


def test_cluster_gate_reuses_golden_namespace_across_calls(
    monkeypatch: pytest.MonkeyPatch,
    package_root: Path,
) -> None:
    from src import refactor_parity_gate as gate

    exec_calls: list[str] = []
    real_exec = gate.exec_internals_module

    def tracking_exec(source: str, *, package_root):
        exec_calls.append(source)
        return real_exec(source, package_root=package_root)

    monkeypatch.setattr(gate, "exec_internals_module", tracking_exec)
    gate._golden_namespace.cache_clear()

    check_cluster_parity(
        pristine_source=PRISTINE_CLUSTER,
        current_source=PRISTINE_CLUSTER,
        response=_cluster_response(CORRECT_CLUSTER_SOURCE),
        input_vectors=CLUSTER_INPUTS,
        package_root=package_root,
    )
    check_cluster_parity(
        pristine_source=PRISTINE_CLUSTER,
        current_source=PRISTINE_CLUSTER,
        response=_cluster_response(CORRECT_CLUSTER_SOURCE),
        input_vectors=CLUSTER_INPUTS,
        package_root=package_root,
    )

    pristine_execs = [source for source in exec_calls if source == PRISTINE_CLUSTER]
    assert len(pristine_execs) == 1


def test_cluster_gate_passes_for_semantics_preserving_refactor(
    package_root: Path,
) -> None:
    check_cluster_parity(
        pristine_source=PRISTINE_CLUSTER,
        current_source=PRISTINE_CLUSTER,
        response=_cluster_response(CORRECT_CLUSTER_SOURCE),
        input_vectors=CLUSTER_INPUTS,
        package_root=package_root,
    )


def test_cluster_gate_rejects_semantics_breaking_refactor(package_root: Path) -> None:
    with pytest.raises(ParityError) as excinfo:
        check_cluster_parity(
            pristine_source=PRISTINE_CLUSTER,
            current_source=PRISTINE_CLUSTER,
            response=_cluster_response(BROKEN_CLUSTER_SOURCE),
            input_vectors=CLUSTER_INPUTS,
            package_root=package_root,
        )
    message = str(excinfo.value)
    assert "combined_input_passthrough" in message
    assert "Engine!D6" in message
    assert "20.0" in message


def test_cluster_gate_converts_runtime_error_to_parity_error(
    package_root: Path,
) -> None:
    with pytest.raises(ParityError) as excinfo:
        check_cluster_parity(
            pristine_source=PRISTINE_CLUSTER,
            current_source=PRISTINE_CLUSTER,
            response=_cluster_response(CRASHING_CLUSTER_SOURCE),
            input_vectors=CLUSTER_INPUTS,
            package_root=package_root,
        )
    message = str(excinfo.value)
    assert "combined_input_passthrough" in message
    assert "TypeError" in message


def test_cluster_gate_surfaces_missing_symbol_as_retryable_parity_error(
    package_root: Path,
) -> None:
    with pytest.raises(ParityError) as excinfo:
        check_cluster_parity(
            pristine_source=PRISTINE_CLUSTER,
            current_source=PRISTINE_CLUSTER,
            response=_cluster_response(MISSING_SYMBOL_CLUSTER_SOURCE),
            input_vectors=CLUSTER_INPUTS,
            package_root=package_root,
        )
    message = str(excinfo.value)
    assert "refactored symbol combined_input_passthrough could not be loaded" in message
    assert "KeyError" in message


def _mechanical_cluster_module(helper_source: str) -> str:
    return RUNTIME_IMPORT + "\n" + helper_source + "\n" + RESOLVER_SECTION


def test_batched_mechanical_parity_passes_for_correct_units(package_root: Path) -> None:
    from src.refactor_parity_gate import (
        MechanicalParityUnit,
        check_batched_mechanical_parity,
    )

    units = (
        MechanicalParityUnit(
            unit_id="cluster:1",
            helper_name="combined_input_passthrough",
            kind="cluster",
            member_checks=(
                ("Engine!C6", {"time_period": 1}),
                ("Engine!D6", {"time_period": 2}),
            ),
        ),
    )
    check_batched_mechanical_parity(
        pristine_source=PRISTINE_CLUSTER,
        mechanical_source=_mechanical_cluster_module(CORRECT_CLUSTER_SOURCE),
        units=units,
        input_vectors=CLUSTER_INPUTS,
        package_root=package_root,
    )


def test_batched_mechanical_parity_names_failing_unit(package_root: Path) -> None:
    from src.refactor_parity_gate import (
        MechanicalParityUnit,
        check_batched_mechanical_parity,
    )

    units = (
        MechanicalParityUnit(
            unit_id="cluster:1",
            helper_name="combined_input_passthrough",
            kind="cluster",
            member_checks=(
                ("Engine!C6", {"time_period": 1}),
                ("Engine!D6", {"time_period": 2}),
            ),
        ),
    )
    with pytest.raises(ParityError) as excinfo:
        check_batched_mechanical_parity(
            pristine_source=PRISTINE_CLUSTER,
            mechanical_source=_mechanical_cluster_module(BROKEN_CLUSTER_SOURCE),
            units=units,
            input_vectors=CLUSTER_INPUTS,
            package_root=package_root,
        )
    assert "cluster:1" in str(excinfo.value)


def _batched_cluster_unit():
    from src.refactor_parity_gate import MechanicalParityUnit

    return MechanicalParityUnit(
        unit_id="cluster:1",
        helper_name="combined_input_passthrough",
        kind="cluster",
        member_checks=(
            ("Engine!C6", {"time_period": 1}),
            ("Engine!D6", {"time_period": 2}),
        ),
    )


def test_batched_mechanical_parity_logs_phases_and_completion(
    caplog: pytest.LogCaptureFixture,
    package_root: Path,
) -> None:
    from src.refactor_parity_gate import (
        _golden_namespace,
        check_batched_mechanical_parity,
    )

    _golden_namespace.cache_clear()
    units = (_batched_cluster_unit(),)
    planned_checks = 2 * len(CLUSTER_INPUTS)

    with caplog.at_level(logging.INFO, logger="src.refactor_parity_gate"):
        check_batched_mechanical_parity(
            pristine_source=PRISTINE_CLUSTER,
            mechanical_source=_mechanical_cluster_module(CORRECT_CLUSTER_SOURCE),
            units=units,
            input_vectors=CLUSTER_INPUTS,
            package_root=package_root,
        )

    messages = [record.message for record in caplog.records]
    start = next(message for message in messages if "parity gate starting" in message)
    assert "units=1" in start
    assert "member_checks=2" in start
    assert f"input_vectors={len(CLUSTER_INPUTS)}" in start
    assert f"planned_checks={planned_checks}" in start
    assert "atol=" in start
    assert "pristine_chars=" in start
    assert "mechanical_chars=" in start

    assert any(
        "golden exec" in message and "cache miss" in message for message in messages
    )
    assert any("candidate exec" in message for message in messages)
    assert any("parity progress" in message for message in messages)

    complete = next(
        message for message in messages if "parity gate complete" in message
    )
    assert f"checks={planned_checks}" in complete
    assert "mismatches=0" in complete
    assert "eval=" in complete


def test_batched_mechanical_parity_logs_failure_summary(
    caplog: pytest.LogCaptureFixture,
    package_root: Path,
) -> None:
    from src.refactor_parity_gate import check_batched_mechanical_parity

    units = (_batched_cluster_unit(),)
    with (
        caplog.at_level(logging.INFO, logger="src.refactor_parity_gate"),
        pytest.raises(ParityError),
    ):
        check_batched_mechanical_parity(
            pristine_source=PRISTINE_CLUSTER,
            mechanical_source=_mechanical_cluster_module(BROKEN_CLUSTER_SOURCE),
            units=units,
            input_vectors=CLUSTER_INPUTS,
            package_root=package_root,
        )

    assert any("parity gate starting" in record.message for record in caplog.records)
    failure = next(
        record.message
        for record in caplog.records
        if "parity gate failed" in record.message
    )
    assert "cluster:1" in failure
    assert "mismatches=" in failure


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


def _singleton_context(package_root: Path) -> SingletonRefactorContext:
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
        allowed_runtime_symbols=allowed_runtime_symbols(package_root),
        naming_hints={},
        expected_helper_name="initial_value",
        package_root=package_root,
    )


def _singleton_response(symbol_source: str) -> SingletonRefactorResponse:
    return SingletonRefactorResponse(
        symbol_name="initial_value",
        symbol_docstring=SINGLETON_DOCSTRING,
        symbol_source=symbol_source,
    )


SINGLETON_INPUTS = [{"Inputs!B2": 7.0}, {"Inputs!B2": 11.0}]


def test_singleton_gate_passes_for_semantics_preserving_refactor(
    package_root: Path,
) -> None:
    check_singleton_parity(
        pristine_source=PRISTINE_SINGLETON,
        current_source=PRISTINE_SINGLETON,
        response=_singleton_response(CORRECT_SINGLETON_SOURCE),
        ctx=_singleton_context(package_root),
        input_vectors=SINGLETON_INPUTS,
        package_root=package_root,
    )


def test_singleton_gate_rejects_semantics_breaking_refactor(package_root: Path) -> None:
    with pytest.raises(ParityError) as excinfo:
        check_singleton_parity(
            pristine_source=PRISTINE_SINGLETON,
            current_source=PRISTINE_SINGLETON,
            response=_singleton_response(BROKEN_SINGLETON_SOURCE),
            ctx=_singleton_context(package_root),
            input_vectors=SINGLETON_INPUTS,
            package_root=package_root,
        )
    assert "Inputs!B6" in str(excinfo.value)


def test_singleton_gate_converts_runtime_error_to_parity_error(
    package_root: Path,
) -> None:
    with pytest.raises(ParityError) as excinfo:
        check_singleton_parity(
            pristine_source=PRISTINE_SINGLETON,
            current_source=PRISTINE_SINGLETON,
            response=_singleton_response(CRASHING_SINGLETON_SOURCE),
            ctx=_singleton_context(package_root),
            input_vectors=SINGLETON_INPUTS,
            package_root=package_root,
        )
    message = str(excinfo.value)
    assert "initial_value(ctx)" in message
    assert "TypeError" in message


def test_singleton_gate_passes_when_both_sides_return_excel_error(
    package_root: Path,
) -> None:
    check_singleton_parity(
        pristine_source=PRISTINE_XLERROR_SINGLETON,
        current_source=PRISTINE_XLERROR_SINGLETON,
        response=_singleton_response(CORRECT_XLERROR_SINGLETON_SOURCE),
        ctx=_singleton_context(package_root),
        input_vectors=SINGLETON_INPUTS,
        package_root=package_root,
    )


def test_evaluate_golden_normalizes_excel_errors(package_root: Path) -> None:
    from src.refactor_parity_gate import _runtime

    runtime = _runtime(str(package_root.resolve()))

    result = _evaluate_golden(
        lambda: (_ for _ in ()).throw(runtime.XlErrorException(runtime.XlError.VALUE)),
        package_root=package_root,
    )
    assert result is runtime.XlError.VALUE


def test_evaluate_golden_propagates_non_excel_exceptions(package_root: Path) -> None:
    with pytest.raises(RuntimeError, match="pipeline defect"):
        _evaluate_golden(
            lambda: (_ for _ in ()).throw(RuntimeError("pipeline defect")),
            package_root=package_root,
        )


def test_evaluate_candidate_normalizes_excel_errors(package_root: Path) -> None:
    from src.refactor_parity_gate import _runtime

    runtime = _runtime(str(package_root.resolve()))

    result = _evaluate_candidate(
        lambda: (_ for _ in ()).throw(runtime.XlErrorException(runtime.XlError.NA)),
        call="helper(ctx)",
        package_root=package_root,
    )
    assert result is runtime.XlError.NA


def test_evaluate_candidate_wraps_non_excel_exceptions_with_call_site(
    package_root: Path,
) -> None:
    with pytest.raises(ParityError) as excinfo:
        _evaluate_candidate(
            lambda: (_ for _ in ()).throw(ValueError("bad helper")),
            call="helper(ctx, time_period=1)",
            package_root=package_root,
        )
    message = str(excinfo.value)
    assert "helper(ctx, time_period=1)" in message
    assert "ValueError" in message


def test_load_candidate_surfaces_compile_errors_as_parity_error(
    package_root: Path,
) -> None:
    with pytest.raises(ParityError) as excinfo:
        _load_candidate("def broken(\n", "broken", package_root=package_root)
    message = str(excinfo.value)
    assert "refactored symbol broken could not be loaded" in message
    assert "SyntaxError" in message


def test_sample_input_vectors_includes_numeric_boundaries_and_zero() -> None:
    constraints = {
        "Inputs!C16": Annotated[float, RealBetween(-10.0, 15.0)],
        "Inputs!D17": Annotated[float, RealBetween(0.0, 20.0)],
        "Inputs!B21": Annotated[int, Between(1, 5)],
    }
    default_inputs = {
        "Inputs!C16": 3.5,
        "Inputs!D17": 4.0,
        "Inputs!B21": 2,
    }

    vectors = sample_input_vectors(
        constraints=constraints,
        default_inputs=default_inputs,
        constants={},
        count=0,
        seed=0,
    )

    assert vectors[0] == default_inputs
    probe_vectors = vectors[1:]
    assert len(probe_vectors) == 7

    c16_probes = [
        vector["Inputs!C16"]
        for vector in probe_vectors
        if vector["Inputs!D17"] == 4.0 and vector["Inputs!B21"] == 2
    ]
    assert c16_probes == [-10.0, 15.0, 0.0]

    d17_probes = [
        vector["Inputs!D17"]
        for vector in probe_vectors
        if vector["Inputs!C16"] == 3.5 and vector["Inputs!B21"] == 2
    ]
    assert d17_probes == [0.0, 20.0]

    b21_probes = [
        vector["Inputs!B21"]
        for vector in probe_vectors
        if vector["Inputs!C16"] == 3.5 and vector["Inputs!D17"] == 4.0
    ]
    assert b21_probes == [1, 5]


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
    assert len(vectors) == 11

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


RECURRENCE_PRISTINE = (
    RUNTIME_IMPORT
    + '''
# --- Formula cell functions ---
def cell_engine_c10(ctx):
    """Anchor year."""
    return 1

def cell_engine_d10(ctx):
    return xl_number(xl_eval(ctx, 'Engine!C10', cell_engine_c10)) + 1

def cell_engine_e10(ctx):
    return xl_number(xl_eval(ctx, 'Engine!D10', cell_engine_d10)) + 1

'''
    + RESOLVER_SECTION
)

RECURRENCE_HELPER = '''\
def accum_path(ctx, time_period: int):
    """Accumulate one per period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Period index.

    Returns:
        Accumulated value.
    """
    if time_period <= 1:
        return 1
    return accum_path(ctx, time_period=time_period - 1) + 1
'''


def _recurrence_mechanical_module() -> str:
    return (
        RUNTIME_IMPORT
        + "\n# --- Formula cell functions ---\n"
        + RECURRENCE_HELPER
        + "\n"
        + RESOLVER_SECTION
    )


def test_batched_mechanical_parity_warm_recurrence_is_incremental(
    package_root: Path,
) -> None:
    """Late-horizon members must not re-walk O(years) under a warm EvalContext."""
    from src.helper_memoization import memoize_namespace_helpers
    from src.refactor_parity_gate import (
        MechanicalParityUnit,
        _runtime,
        check_batched_mechanical_parity,
        exec_internals_module,
        make_eval_context,
    )

    runtime = _runtime(str(package_root.resolve()))
    horizon = 80
    instrumented = '''\
def accum_path(ctx, time_period: int):
    """Accumulate one per period.

    Args:
        ctx: Workbook evaluation context.
        time_period: Period index.

    Returns:
        Accumulated value.
    """
    ctx.inputs['__calls__'] = ctx.inputs.get('__calls__', 0) + 1
    if time_period <= 1:
        return 1
    return accum_path(ctx, time_period=time_period - 1) + 1
'''
    module = (
        RUNTIME_IMPORT
        + "\n# --- Formula cell functions ---\n"
        + instrumented
        + "\n"
        + RESOLVER_SECTION
    )

    bare_ns = exec_internals_module(module, package_root=package_root)
    bare_ctx = make_eval_context(bare_ns, {"__calls__": 0}, package_root=package_root)
    assert bare_ns["accum_path"](bare_ctx, time_period=horizon) == horizon
    bare_cold_calls = bare_ctx.inputs["__calls__"]
    bare_ctx.inputs["__calls__"] = 0
    assert bare_ns["accum_path"](bare_ctx, time_period=horizon + 1) == horizon + 1
    bare_warm_calls = bare_ctx.inputs["__calls__"]
    # Without memoization each call re-enters every prior year.
    assert bare_cold_calls == horizon
    assert bare_warm_calls == horizon + 1

    memo_ns = exec_internals_module(module, package_root=package_root)
    memoize_namespace_helpers(memo_ns, ("accum_path",), runtime=runtime)
    memo_ctx = make_eval_context(memo_ns, {"__calls__": 0}, package_root=package_root)
    assert memo_ns["accum_path"](memo_ctx, time_period=horizon) == horizon
    memo_cold_calls = memo_ctx.inputs["__calls__"]
    memo_ctx.inputs["__calls__"] = 0
    assert memo_ns["accum_path"](memo_ctx, time_period=horizon + 1) == horizon + 1
    memo_warm_calls = memo_ctx.inputs["__calls__"]
    assert memo_cold_calls == horizon
    # Warm late-horizon step is one body entry, not another O(years) walk.
    assert memo_warm_calls == 1

    units = (
        MechanicalParityUnit(
            unit_id="cluster:recurrence",
            helper_name="accum_path",
            kind="cluster",
            member_checks=(
                ("Engine!C10", {"time_period": 1}),
                ("Engine!D10", {"time_period": 2}),
                ("Engine!E10", {"time_period": 3}),
            ),
        ),
    )
    check_batched_mechanical_parity(
        pristine_source=RECURRENCE_PRISTINE,
        mechanical_source=_recurrence_mechanical_module(),
        units=units,
        input_vectors=[{}],
        package_root=package_root,
    )


def test_batched_mechanical_parity_still_reports_recurrence_mismatch(
    package_root: Path,
) -> None:
    from src.refactor_parity_gate import (
        MechanicalParityUnit,
        check_batched_mechanical_parity,
    )

    broken = RECURRENCE_HELPER.replace(
        "return accum_path(ctx, time_period=time_period - 1) + 1",
        "return accum_path(ctx, time_period=time_period - 1) + 2",
    )
    mechanical = (
        RUNTIME_IMPORT
        + "\n# --- Formula cell functions ---\n"
        + broken
        + "\n"
        + RESOLVER_SECTION
    )
    units = (
        MechanicalParityUnit(
            unit_id="cluster:recurrence",
            helper_name="accum_path",
            kind="cluster",
            member_checks=(
                ("Engine!C10", {"time_period": 1}),
                ("Engine!D10", {"time_period": 2}),
                ("Engine!E10", {"time_period": 3}),
            ),
        ),
    )
    with pytest.raises(ParityError) as excinfo:
        check_batched_mechanical_parity(
            pristine_source=RECURRENCE_PRISTINE,
            mechanical_source=mechanical,
            units=units,
            input_vectors=[{}],
            package_root=package_root,
        )
    assert "cluster:recurrence" in str(excinfo.value)
