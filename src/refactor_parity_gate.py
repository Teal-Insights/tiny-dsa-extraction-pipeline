"""In-loop behavioral parity gate for the internals refactor.

The structural validators in :mod:`src.internals_refactor` check that a refactor
response is well-formed, well-named, and calls only allowed symbols, but they
cannot see whether the rewritten helper still computes the same numbers as the
cells it replaces. This module closes that gap: after a cluster or singleton is
refactored, it executes the candidate ``internals.py`` and compares the values
produced by the new helper against the pristine pre-refactor cell semantics
across several input vectors.

A divergence raises :class:`ParityError`. When the gate runs inside the LLM
``post_validate`` hook, that exception rolls back the transaction (nothing is
written or cached) and re-prompts the model with the diff as feedback.

The golden oracle is the pristine generated ``internals.py`` captured once before
any refactoring runs. Member cells are evaluated through the pristine module by
address (their ``cell_*`` functions still exist there); the candidate is checked
by calling the new helper directly with each member's key combination, because
the member ``cell_*`` functions no longer exist after the collapse.
"""

from __future__ import annotations

import ast
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence, get_args, get_origin

from excel_grapher.core.cell_types import Between, RealBetween

from src.pipeline_context import require_pipeline_config
from src.runtime_symbols import allowed_runtime_symbols

if TYPE_CHECKING:
    from src.internals_refactor import (
        ClusterRefactorContext,
        ClusterRefactorResponse,
        SingletonRefactorContext,
        SingletonRefactorResponse,
    )

PARITY_ATOL = 1e-6
DEFAULT_SAMPLE_COUNT = 8
DEFAULT_SAMPLE_SEED = 0
_MAX_REPORTED_MISMATCHES = 10

repo_root = Path(__file__).resolve().parents[1]


def _runtime_path() -> Path:
    config = require_pipeline_config()
    return config.package_root / "runtime.py"


def _data_path() -> Path:
    config = require_pipeline_config()
    return config.package_root / "data.py"


InputVector = Mapping[str, Any]


class ParityError(ValueError):
    """Raised when a refactored helper diverges from the original cell semantics."""


@dataclass(frozen=True)
class _Mismatch:
    address: str
    call: str
    expected: object
    actual: object
    vector_index: int


@lru_cache(maxsize=1)
def _runtime() -> ModuleType:
    """Load the exported runtime module in isolation (no package side effects)."""
    runtime_path = _runtime_path()
    spec = importlib_util.spec_from_file_location(
        "_exported_runtime_for_parity", runtime_path
    )
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"Could not load runtime module from {runtime_path}")
    module = importlib_util.module_from_spec(spec)
    # Register before exec so the module's dataclasses can resolve their own
    # string annotations via sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _strip_runtime_import(source: str) -> str:
    """Remove the ``from .runtime import (...)`` block so the source execs standalone."""
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans = [
        (node.lineno - 1, node.end_lineno or node.lineno)
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "runtime"
    ]
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    return "".join(lines)


def exec_internals_module(source: str) -> dict[str, Any]:
    """Execute an ``internals.py`` source string with runtime symbols injected."""
    runtime = _runtime()
    namespace: dict[str, Any] = {
        name: getattr(runtime, name) for name in allowed_runtime_symbols()
    }
    namespace["__name__"] = "_exported_internals_parity"
    compiled = compile(_strip_runtime_import(source), "<internals-parity>", "exec")
    exec(compiled, namespace)
    return namespace


def make_eval_context(namespace: dict[str, Any], inputs: InputVector) -> Any:
    """Build an ``EvalContext`` bound to a module namespace's resolver."""
    runtime = _runtime()
    return runtime.EvalContext(
        inputs=runtime.coerce_inputs_dict(dict(inputs)),
        resolver=namespace["_resolve_formula"],
    )


def _evaluate_candidate(thunk: Callable[[], Any], *, call: str) -> Any:
    """Evaluate a candidate helper call, normalizing outcomes for comparison.

    A raised :class:`XlErrorException` is a legitimate Excel-error result and is
    returned as its ``XlError`` code so it can be compared against the oracle.
    Any other exception is a defect in the generated code; it is re-raised as a
    :class:`ParityError` so the LLM retry loop re-prompts the model with the
    failure instead of the exception aborting the whole pipeline.
    """
    runtime = _runtime()
    try:
        return thunk()
    except runtime.XlErrorException as error:
        return error.code
    except Exception as error:
        raise ParityError(
            f"{call} raised {type(error).__name__} during evaluation: {error}. "
            "The refactored helper must execute without error and reproduce the "
            "original cell value; fix the body so the call succeeds while "
            "preserving the semantics of the per-member Excel formulas."
        ) from error


def _evaluate_golden(thunk: Callable[[], Any]) -> Any:
    """Evaluate the pristine oracle, returning an ``XlError`` code for Excel errors.

    The oracle is trusted, so non-Excel exceptions are left to propagate: they
    indicate a defect in the pipeline itself rather than in a candidate helper.
    """
    runtime = _runtime()
    try:
        return thunk()
    except runtime.XlErrorException as error:
        return error.code


def _load_candidate(source: str, symbol_name: str) -> tuple[dict[str, Any], Any]:
    """Execute a candidate ``internals.py`` and fetch its refactored symbol.

    A failure to compile, exec, or locate the symbol is treated as a candidate
    defect and surfaced as a retryable :class:`ParityError`.
    """
    try:
        namespace = exec_internals_module(source)
        return namespace, namespace[symbol_name]
    except Exception as error:
        raise ParityError(
            f"refactored symbol {symbol_name} could not be loaded: "
            f"{type(error).__name__}: {error}. Emit a helper whose module "
            "parses, imports, and exposes the named function cleanly."
        ) from error


def _values_close(expected: object, actual: object, atol: float) -> bool:
    runtime = _runtime()
    if isinstance(expected, runtime.XlError) or isinstance(actual, runtime.XlError):
        return expected == actual
    if expected is None or actual is None:
        return expected == actual
    try:
        return abs(float(expected) - float(actual)) <= atol  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return expected == actual


def _format_call(name: str, literals: Mapping[str, object]) -> str:
    if not literals:
        return f"{name}(ctx)"
    rendered = ", ".join(f"{key}={value!r}" for key, value in literals.items())
    return f"{name}(ctx, {rendered})"


def _format_message(
    name: str,
    mismatches: Sequence[_Mismatch],
    total_checks: int,
    atol: float,
) -> str:
    lines = [
        f"helper {name} diverges from the original cell semantics "
        f"({len(mismatches)} of {total_checks} checks failed):"
    ]
    for mismatch in mismatches[:_MAX_REPORTED_MISMATCHES]:
        lines.append(
            f"  {mismatch.address} via {mismatch.call}: got {mismatch.actual!r}, "
            f"expected {mismatch.expected!r} [input vector #{mismatch.vector_index}]"
        )
    if len(mismatches) > _MAX_REPORTED_MISMATCHES:
        lines.append(f"  ... and {len(mismatches) - _MAX_REPORTED_MISMATCHES} more")
    lines.append(
        f"The refactored helper must reproduce each member cell's original computed "
        f"value exactly (atol={atol:g}) across all inputs. Re-derive the body from the "
        f"per-member Excel formulas in the Note section; do not substitute a different "
        f"helper or change which input rows are read."
    )
    return "\n".join(lines)


def check_cluster_parity(
    *,
    pristine_source: str,
    current_source: str,
    response: ClusterRefactorResponse,
    input_vectors: Sequence[InputVector],
    ctx: ClusterRefactorContext | None = None,
    atol: float = PARITY_ATOL,
) -> None:
    """Verify a cluster helper reproduces each member cell's pristine value.

    Args:
        pristine_source: Original generated ``internals.py`` before any refactor.
        current_source: ``internals.py`` as it exists just before this collapse.
        response: The candidate cluster refactor response to gate.
        input_vectors: Input mappings (address -> value) to evaluate under.
        ctx: Cluster context, threaded to ``apply_refactor_plan`` (unused there).
        atol: Absolute tolerance for value comparison.

    Raises:
        ParityError: If any member's helper value diverges from the original.
    """
    from src.internals_refactor import _parameter_literals, apply_refactor_plan

    runtime = _runtime()
    candidate_source = apply_refactor_plan(current_source, response, ctx)
    golden_ns = exec_internals_module(pristine_source)
    candidate_ns, helper = _load_candidate(candidate_source, response.helper_name)

    mismatches: list[_Mismatch] = []
    for index, inputs in enumerate(input_vectors):
        for entry in response.member_keys:
            literals = _parameter_literals(response.parameters, entry.keys)
            golden_ctx = make_eval_context(golden_ns, inputs)
            expected = _evaluate_golden(
                lambda eval_ctx=golden_ctx, address=entry.address: runtime.xl_cell(
                    eval_ctx, address
                )
            )
            candidate_ctx = make_eval_context(candidate_ns, inputs)
            call = _format_call(response.helper_name, literals)
            actual = _evaluate_candidate(
                lambda fn=helper, eval_ctx=candidate_ctx, kwargs=literals: fn(
                    eval_ctx, **kwargs
                ),
                call=call,
            )
            if not _values_close(expected, actual, atol):
                mismatches.append(
                    _Mismatch(
                        address=entry.address,
                        call=call,
                        expected=expected,
                        actual=actual,
                        vector_index=index,
                    )
                )

    if mismatches:
        total = len(input_vectors) * len(response.member_keys)
        raise ParityError(
            _format_message(response.helper_name, mismatches, total, atol)
        )


def check_singleton_parity(
    *,
    pristine_source: str,
    current_source: str,
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
    input_vectors: Sequence[InputVector],
    atol: float = PARITY_ATOL,
) -> None:
    """Verify a renamed singleton reproduces its cell's pristine value.

    Args:
        pristine_source: Original generated ``internals.py`` before any refactor.
        current_source: ``internals.py`` as it exists just before this rename.
        response: The candidate singleton refactor response to gate.
        ctx: Singleton context providing the covered address.
        input_vectors: Input mappings (address -> value) to evaluate under.
        atol: Absolute tolerance for value comparison.

    Raises:
        ParityError: If the renamed symbol's value diverges from the original.
    """
    from src.internals_refactor import apply_singleton_refactor_plan

    runtime = _runtime()
    candidate_source, _ = apply_singleton_refactor_plan(current_source, response, ctx)
    golden_ns = exec_internals_module(pristine_source)
    candidate_ns, symbol = _load_candidate(candidate_source, response.symbol_name)
    call = f"{response.symbol_name}(ctx)"

    mismatches: list[_Mismatch] = []
    address = ctx.address
    for index, inputs in enumerate(input_vectors):
        golden_ctx = make_eval_context(golden_ns, inputs)
        expected = _evaluate_golden(
            lambda eval_ctx=golden_ctx: runtime.xl_cell(eval_ctx, address)
        )
        candidate_ctx = make_eval_context(candidate_ns, inputs)
        actual = _evaluate_candidate(
            lambda fn=symbol, eval_ctx=candidate_ctx: fn(eval_ctx),
            call=call,
        )
        if not _values_close(expected, actual, atol):
            mismatches.append(
                _Mismatch(
                    address=ctx.address,
                    call=call,
                    expected=expected,
                    actual=actual,
                    vector_index=index,
                )
            )

    if mismatches:
        raise ParityError(
            _format_message(response.symbol_name, mismatches, len(input_vectors), atol)
        )


@lru_cache(maxsize=1)
def _dist_data() -> ModuleType:
    """Load the exported ``data.py`` (DEFAULT_INPUTS/CONSTANTS) in isolation."""
    data_path = _data_path()
    spec = importlib_util.spec_from_file_location(
        "_exported_data_for_parity", data_path
    )
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"Could not load data module from {data_path}")
    module = importlib_util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_default_input_vectors(
    *,
    count: int = DEFAULT_SAMPLE_COUNT,
    seed: int = DEFAULT_SAMPLE_SEED,
) -> list[dict[str, object]]:
    """Build gate input vectors from pipeline constraints and exported defaults."""
    from src.pipeline_context import require_pipeline_config

    config = require_pipeline_config()
    data = _dist_data()
    return sample_input_vectors(
        constraints=config.constraints,
        default_inputs=data.DEFAULT_INPUTS,
        constants=data.CONSTANTS,
        count=count,
        seed=seed,
    )


def _sample_constraint(
    annotation: object, default: object, rng: random.Random
) -> object:
    """Draw a value within a constraint annotation's domain, or fall back to default."""
    metadata = getattr(annotation, "__metadata__", None)
    if metadata is not None:
        for meta in metadata:
            if isinstance(meta, RealBetween):
                return rng.uniform(meta.min, meta.max)
            if isinstance(meta, Between):
                return rng.randint(meta.min, meta.max)
        return default
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        if len(choices) <= 1:
            return default
        return rng.choice(choices)
    return default


def sample_input_vectors(
    *,
    constraints: Mapping[str, object],
    default_inputs: Mapping[str, object],
    constants: Mapping[str, object],
    count: int = DEFAULT_SAMPLE_COUNT,
    seed: int = DEFAULT_SAMPLE_SEED,
) -> list[dict[str, object]]:
    """Build input vectors: the canonical default plus seeded random draws.

    The first vector is ``default_inputs`` merged with ``constants``. Each
    additional vector keeps the constants fixed and perturbs every default input
    within its declared constraint domain. Sampling is deterministic for a given
    seed so the gate is reproducible.
    """
    base = {**default_inputs, **constants}
    vectors: list[dict[str, object]] = [dict(base)]
    rng = random.Random(seed)
    sampled_addresses = [
        address for address in default_inputs if address in constraints
    ]
    for _ in range(count):
        vector = dict(base)
        for address in sampled_addresses:
            vector[address] = _sample_constraint(
                constraints[address], default_inputs[address], rng
            )
        vectors.append(vector)
    return vectors
