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
import logging
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence, get_args, get_origin

from excel_grapher.core.cell_types import Between, RealBetween

from src.helper_memoization import (
    clear_side_helper_memos,
    install_helper_memoization,
    memoize_namespace_helpers,
)
from src.runtime_symbols import (
    discover_allowed_reader_symbols,
    discover_allowed_runtime_symbols,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.internals_refactor import (
        ClusterRefactorContext,
        ClusterRefactorResponse,
        SingletonRefactorContext,
        SingletonRefactorResponse,
    )

# Bump when batched / per-unit parity-gate semantics change.
PARITY_GATE_SCHEMA_VERSION = "1.0.0"
PARITY_ATOL = 1e-6
DEFAULT_SAMPLE_COUNT = 8
DEFAULT_SAMPLE_SEED = 0
_MAX_REPORTED_MISMATCHES = 10
# Progress cadence for the batched mechanical gate. Full runs can check
# ~150k member×vector combinations; log often enough for operators to see
# movement without flooding (every N checks, every vector, or every M seconds).
_BATCHED_PROGRESS_EVERY_CHECKS = 5_000
_BATCHED_PROGRESS_EVERY_SECONDS = 30.0

repo_root = Path(__file__).resolve().parents[1]


def _resolved_package_root(package_root: Path) -> str:
    return str(package_root.resolve())


def _runtime_path(package_root: Path) -> Path:
    return Path(_resolved_package_root(package_root)) / "runtime.py"


def _readers_path(package_root: Path) -> Path:
    return Path(_resolved_package_root(package_root)) / "_readers.py"


def _data_path(package_root: Path) -> Path:
    return Path(_resolved_package_root(package_root)) / "data.py"


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


@lru_cache(maxsize=8)
def _runtime(package_root: str) -> ModuleType:
    """Load the exported runtime module in isolation (no package side effects)."""
    runtime_path = Path(package_root) / "runtime.py"
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
    # Older embedded runtimes lack xl_helper; install a compatible polyfill so
    # mechanical recurrence helpers can share work under a warm EvalContext.
    install_helper_memoization(module)
    return module


def clear_parity_runtime_caches() -> None:
    """Drop cached runtime/oracle namespaces (e.g. after patching ``runtime.py``)."""
    _runtime.cache_clear()
    _readers_namespace.cache_clear()
    _golden_namespace.cache_clear()
    _dist_data.cache_clear()
    clear_side_helper_memos()


def _strip_package_relative_imports(source: str) -> str:
    """Remove ``from .runtime`` / ``from ._readers`` imports for standalone exec."""
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans = [
        (node.lineno - 1, node.end_lineno or node.lineno)
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module in {"runtime", "_readers"}
    ]
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    return "".join(lines)


@lru_cache(maxsize=8)
def _readers_namespace(package_root: str) -> dict[str, Any]:
    """Load exported ``_readers.py`` with runtime symbols injected."""
    readers_path = Path(package_root) / "_readers.py"
    reader_names = discover_allowed_reader_symbols(readers_path)
    if not reader_names:
        return {}
    runtime = _runtime(package_root)
    namespace: dict[str, Any] = {
        name: getattr(runtime, name)
        for name in dir(runtime)
        if not name.startswith("__")
    }
    namespace["__name__"] = "_exported_readers_parity"
    source = readers_path.read_text(encoding="utf-8")
    compiled = compile(
        _strip_package_relative_imports(source),
        "<readers-parity>",
        "exec",
    )
    exec(compiled, namespace)
    return {name: namespace[name] for name in reader_names}


def exec_internals_module(source: str, *, package_root: Path) -> dict[str, Any]:
    """Execute an ``internals.py`` source string with runtime/reader symbols injected."""
    root = _resolved_package_root(package_root)
    runtime = _runtime(root)
    namespace: dict[str, Any] = {
        name: getattr(runtime, name)
        for name in discover_allowed_runtime_symbols(_runtime_path(package_root))
    }
    namespace.update(_readers_namespace(root))
    namespace["__name__"] = "_exported_internals_parity"
    compiled = compile(
        _strip_package_relative_imports(source),
        "<internals-parity>",
        "exec",
    )
    exec(compiled, namespace)
    return namespace


@lru_cache(maxsize=8)
def _golden_namespace(pristine_source: str, package_root: str) -> dict[str, Any]:
    """Execute the pristine ``internals.py`` once and reuse its namespace.

    The pristine oracle source is identical for every cluster and singleton in a
    refactor run, so execing the multi-megabyte module inside each gate call
    dominated post-response cost (~4 s per call over 800+ rewrites). The returned
    namespace is only read during evaluation — each parity check builds a fresh
    :class:`EvalContext` (which owns the per-run memoization ``cache``) bound to
    ``namespace["_resolve_formula"]`` — so a single cached exec is safe to share.
    """
    return exec_internals_module(pristine_source, package_root=Path(package_root))


def make_eval_context(
    namespace: dict[str, Any],
    inputs: InputVector,
    *,
    package_root: Path,
) -> Any:
    """Build an ``EvalContext`` bound to a module namespace's resolver."""
    runtime = _runtime(_resolved_package_root(package_root))
    return runtime.EvalContext(
        inputs=runtime.coerce_inputs_dict(dict(inputs)),
        resolver=namespace["_resolve_formula"],
    )


def _evaluate_candidate(
    thunk: Callable[[], Any],
    *,
    call: str,
    package_root: Path,
) -> Any:
    """Evaluate a candidate helper call, normalizing outcomes for comparison.

    A raised :class:`XlErrorException` is a legitimate Excel-error result and is
    returned as its ``XlError`` code so it can be compared against the oracle.
    Any other exception is a defect in the generated code; it is re-raised as a
    :class:`ParityError` so the LLM retry loop re-prompts the model with the
    failure instead of the exception aborting the whole pipeline.
    """
    runtime = _runtime(_resolved_package_root(package_root))
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


def _evaluate_golden(thunk: Callable[[], Any], *, package_root: Path) -> Any:
    """Evaluate the pristine oracle, returning an ``XlError`` code for Excel errors.

    The oracle is trusted, so non-Excel exceptions are left to propagate: they
    indicate a defect in the pipeline itself rather than in a candidate helper.
    """
    runtime = _runtime(_resolved_package_root(package_root))
    try:
        return thunk()
    except runtime.XlErrorException as error:
        return error.code


def _load_candidate(
    source: str,
    symbol_name: str,
    *,
    package_root: Path,
) -> tuple[dict[str, Any], Any]:
    """Execute a candidate ``internals.py`` and fetch its refactored symbol.

    A failure to compile, exec, or locate the symbol is treated as a candidate
    defect and surfaced as a retryable :class:`ParityError`.
    """
    try:
        namespace = exec_internals_module(source, package_root=package_root)
        return namespace, namespace[symbol_name]
    except Exception as error:
        raise ParityError(
            f"refactored symbol {symbol_name} could not be loaded: "
            f"{type(error).__name__}: {error}. Emit a helper whose module "
            "parses, imports, and exposes the named function cleanly."
        ) from error


def _values_close(
    expected: object,
    actual: object,
    atol: float,
    *,
    package_root: Path,
) -> bool:
    runtime = _runtime(_resolved_package_root(package_root))
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
    package_root: Path,
    ctx: ClusterRefactorContext | None = None,
    atol: float = PARITY_ATOL,
) -> None:
    """Verify a cluster helper reproduces each member cell's pristine value.

    Args:
        pristine_source: Original generated ``internals.py`` before any refactor.
        current_source: ``internals.py`` as it exists just before this collapse.
        response: The candidate cluster refactor response to gate.
        input_vectors: Input mappings (address -> value) to evaluate under.
        package_root: Exported package directory containing ``runtime.py``.
        ctx: Cluster context, threaded to ``apply_refactor_plan`` (unused there).
        atol: Absolute tolerance for value comparison.

    Raises:
        ParityError: If any member's helper value diverges from the original.
    """
    from src.internals_refactor import _parameter_literals, apply_refactor_plan

    root = _resolved_package_root(package_root)
    runtime = _runtime(root)
    candidate_source = apply_refactor_plan(current_source, response, ctx)
    golden_ns = _golden_namespace(pristine_source, root)
    candidate_ns, helper = _load_candidate(
        candidate_source, response.helper_name, package_root=package_root
    )

    mismatches: list[_Mismatch] = []
    for index, inputs in enumerate(input_vectors):
        # Build one evaluation context per input vector and share it across all
        # members. Cluster members are the same formula shape across engine
        # columns/rows, so they resolve overlapping dependency subtrees; a shared
        # ``ctx.cache`` memoizes those once instead of once per member. The
        # resolver is pure for fixed inputs, so cross-member reuse is exact.
        golden_ctx = make_eval_context(golden_ns, inputs, package_root=package_root)
        candidate_ctx = make_eval_context(
            candidate_ns, inputs, package_root=package_root
        )
        for entry in response.member_keys:
            literals = _parameter_literals(response.parameters, entry.keys_dict())
            expected = _evaluate_golden(
                lambda eval_ctx=golden_ctx, address=entry.address: runtime.xl_cell(
                    eval_ctx, address
                ),
                package_root=package_root,
            )
            call = _format_call(response.helper_name, literals)
            actual = _evaluate_candidate(
                lambda fn=helper, eval_ctx=candidate_ctx, kwargs=literals: fn(
                    eval_ctx, **kwargs
                ),
                call=call,
                package_root=package_root,
            )
            if not _values_close(expected, actual, atol, package_root=package_root):
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
    package_root: Path,
    atol: float = PARITY_ATOL,
) -> None:
    """Verify a renamed singleton reproduces its cell's pristine value.

    Args:
        pristine_source: Original generated ``internals.py`` before any refactor.
        current_source: ``internals.py`` as it exists just before this rename.
        response: The candidate singleton refactor response to gate.
        ctx: Singleton context providing the covered address.
        input_vectors: Input mappings (address -> value) to evaluate under.
        package_root: Exported package directory containing ``runtime.py``.
        atol: Absolute tolerance for value comparison.

    Raises:
        ParityError: If the renamed symbol's value diverges from the original.
    """
    from src.internals_refactor import apply_singleton_refactor_plan

    root = _resolved_package_root(package_root)
    runtime = _runtime(root)
    candidate_source, _ = apply_singleton_refactor_plan(current_source, response, ctx)
    golden_ns = _golden_namespace(pristine_source, root)
    candidate_ns, symbol = _load_candidate(
        candidate_source, response.symbol_name, package_root=package_root
    )
    call = f"{response.symbol_name}(ctx)"

    mismatches: list[_Mismatch] = []
    address = ctx.address
    for index, inputs in enumerate(input_vectors):
        golden_ctx = make_eval_context(golden_ns, inputs, package_root=package_root)
        expected = _evaluate_golden(
            lambda eval_ctx=golden_ctx: runtime.xl_cell(eval_ctx, address),
            package_root=package_root,
        )
        candidate_ctx = make_eval_context(
            candidate_ns, inputs, package_root=package_root
        )
        actual = _evaluate_candidate(
            lambda fn=symbol, eval_ctx=candidate_ctx: fn(eval_ctx),
            call=call,
            package_root=package_root,
        )
        if not _values_close(expected, actual, atol, package_root=package_root):
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


@dataclass(frozen=True)
class MechanicalParityUnit:
    """A single mechanical helper to check against the pristine oracle.

    ``member_checks`` pairs each covered cell address with the keyword arguments
    used to invoke the helper for that member. A singleton has exactly one
    member check with empty kwargs; a cluster has one per collapsed member.
    """

    unit_id: str
    helper_name: str
    kind: Literal["cluster", "singleton"]
    member_checks: tuple[tuple[str, Mapping[str, object]], ...]


def check_batched_mechanical_parity(
    *,
    pristine_source: str,
    mechanical_source: str,
    units: Sequence[MechanicalParityUnit],
    input_vectors: Sequence[InputVector],
    package_root: Path,
    atol: float = PARITY_ATOL,
) -> None:
    """Verify every mechanically refactored helper against the pristine oracle.

    Pass 1 rewrites all mechanical units into ``internals.py`` before any LLM
    naming runs, so their behavioral parity can be checked in one batch: the
    mechanical module is exec'd once and each unit's helper is compared against
    the pristine cell semantics across all input vectors. A divergence is a
    mechanical-synthesis defect, not an LLM mistake, so it raises loudly (naming
    the failing units) with no retry.
    """
    if not units:
        return

    member_check_count = sum(len(unit.member_checks) for unit in units)
    planned_checks = member_check_count * len(input_vectors)
    gate_started = time.perf_counter()
    logger.info(
        "batched mechanical parity gate starting: units=%d member_checks=%d "
        "input_vectors=%d planned_checks=%d pristine_chars=%d "
        "mechanical_chars=%d atol=%g",
        len(units),
        member_check_count,
        len(input_vectors),
        planned_checks,
        len(pristine_source),
        len(mechanical_source),
        atol,
    )

    root = _resolved_package_root(package_root)
    runtime = _runtime(root)
    cache_info_before = _golden_namespace.cache_info()
    golden_started = time.perf_counter()
    golden_ns = _golden_namespace(pristine_source, root)
    golden_seconds = time.perf_counter() - golden_started
    golden_cache_hit = _golden_namespace.cache_info().hits > cache_info_before.hits
    logger.info(
        "batched mechanical parity: golden exec %.3fs (%s)",
        golden_seconds,
        "cache hit" if golden_cache_hit else "cache miss",
    )

    candidate_started = time.perf_counter()
    try:
        candidate_ns = exec_internals_module(
            mechanical_source, package_root=package_root
        )
    except Exception as error:
        logger.error(
            "batched mechanical parity gate failed: candidate exec raised "
            "%s after %.3fs",
            type(error).__name__,
            time.perf_counter() - candidate_started,
        )
        raise ParityError(
            "mechanically refactored internals.py could not be loaded: "
            f"{type(error).__name__}: {error}. Mechanical synthesis must emit a "
            "module that parses, imports, and execs cleanly."
        ) from error
    candidate_seconds = time.perf_counter() - candidate_started
    logger.info(
        "batched mechanical parity: candidate exec %.3fs",
        candidate_seconds,
    )

    # Memoize candidate helpers so period-recurrence chains share work across
    # member checks under each vector's EvalContext (library-visible xl_memoize).
    memoize_namespace_helpers(
        candidate_ns,
        (unit.helper_name for unit in units),
        runtime=runtime,
    )

    mismatches_by_unit: dict[str, list[_Mismatch]] = {}
    total_checks = 0
    eval_started = time.perf_counter()
    golden_eval_seconds = 0.0
    candidate_eval_seconds = 0.0
    last_progress_at = eval_started
    last_progress_checks = 0

    def _log_progress(*, vector_index: int) -> None:
        nonlocal last_progress_at, last_progress_checks
        now = time.perf_counter()
        eval_elapsed = now - eval_started
        rate = total_checks / eval_elapsed if eval_elapsed > 0 else 0.0
        logger.info(
            "batched mechanical parity progress: checks=%d/%d "
            "vectors=%d/%d units=%d elapsed=%.1fs rate=%.0f checks/s "
            "golden_eval=%.3fs candidate_eval=%.3fs",
            total_checks,
            planned_checks,
            vector_index + 1,
            len(input_vectors),
            len(units),
            eval_elapsed,
            rate,
            golden_eval_seconds,
            candidate_eval_seconds,
        )
        last_progress_at = now
        last_progress_checks = total_checks

    def _should_log_mid_vector_progress() -> bool:
        checks_since_progress = total_checks - last_progress_checks
        seconds_since_progress = time.perf_counter() - last_progress_at
        return (
            checks_since_progress >= _BATCHED_PROGRESS_EVERY_CHECKS
            or seconds_since_progress >= _BATCHED_PROGRESS_EVERY_SECONDS
        )

    try:
        for index, inputs in enumerate(input_vectors):
            golden_ctx = make_eval_context(golden_ns, inputs, package_root=package_root)
            candidate_ctx = make_eval_context(
                candidate_ns, inputs, package_root=package_root
            )
            for unit in units:
                helper = candidate_ns.get(unit.helper_name)
                if helper is None:
                    logger.error(
                        "batched mechanical parity gate failed: missing helper "
                        "%r for unit %r after %d checks",
                        unit.helper_name,
                        unit.unit_id,
                        total_checks,
                    )
                    raise ParityError(
                        f"mechanical helper {unit.helper_name!r} for unit "
                        f"{unit.unit_id!r} is missing from the refactored module"
                    )
                for address, kwargs in unit.member_checks:
                    total_checks += 1
                    literals = dict(kwargs)
                    golden_call_started = time.perf_counter()
                    expected = _evaluate_golden(
                        lambda eval_ctx=golden_ctx, addr=address: runtime.xl_cell(
                            eval_ctx, addr
                        ),
                        package_root=package_root,
                    )
                    golden_eval_seconds += time.perf_counter() - golden_call_started
                    call = _format_call(unit.helper_name, literals)
                    try:
                        candidate_call_started = time.perf_counter()
                        actual = _evaluate_candidate(
                            lambda fn=helper, eval_ctx=candidate_ctx, kw=literals: fn(
                                eval_ctx, **kw
                            ),
                            call=call,
                            package_root=package_root,
                        )
                        candidate_eval_seconds += (
                            time.perf_counter() - candidate_call_started
                        )
                    except ParityError as error:
                        logger.error(
                            "batched mechanical parity gate failed: unit %r raised "
                            "during evaluation after %d checks",
                            unit.unit_id,
                            total_checks,
                        )
                        raise ParityError(
                            f"mechanical unit {unit.unit_id!r}: {error}"
                        ) from error
                    if not _values_close(
                        expected, actual, atol, package_root=package_root
                    ):
                        mismatches_by_unit.setdefault(unit.unit_id, []).append(
                            _Mismatch(
                                address=address,
                                call=call,
                                expected=expected,
                                actual=actual,
                                vector_index=index,
                            )
                        )
                    if _should_log_mid_vector_progress():
                        _log_progress(vector_index=index)

            # Always emit once per finished input vector so full runs show movement
            # even when each vector stays under the mid-vector thresholds.
            if total_checks != last_progress_checks:
                _log_progress(vector_index=index)
    finally:
        clear_side_helper_memos()
    eval_seconds = time.perf_counter() - eval_started
    mismatch_count = sum(len(items) for items in mismatches_by_unit.values())
    gate_elapsed = time.perf_counter() - gate_started

    if mismatches_by_unit:
        failing_unit_ids = sorted(mismatches_by_unit)
        logger.error(
            "batched mechanical parity gate failed: units=%s mismatches=%d "
            "checks=%d golden_exec=%.3fs candidate_exec=%.3fs "
            "golden_eval=%.3fs candidate_eval=%.3fs eval=%.3fs elapsed=%.3fs",
            failing_unit_ids,
            mismatch_count,
            total_checks,
            golden_seconds,
            candidate_seconds,
            golden_eval_seconds,
            candidate_eval_seconds,
            eval_seconds,
            gate_elapsed,
        )
        lines = [
            "mechanical refactor diverges from the original cell semantics for "
            f"unit(s) {failing_unit_ids}:"
        ]
        for unit_id in failing_unit_ids:
            for mismatch in mismatches_by_unit[unit_id][:_MAX_REPORTED_MISMATCHES]:
                lines.append(
                    f"  [{unit_id}] {mismatch.address} via {mismatch.call}: "
                    f"got {mismatch.actual!r}, expected {mismatch.expected!r} "
                    f"[input vector #{mismatch.vector_index}]"
                )
        lines.append(
            f"Checked {total_checks} member/vector combinations (atol={atol:g}). "
            "Mechanical synthesis must reproduce each cell's pristine value; this "
            "is a synthesis defect, not an LLM naming error."
        )
        raise ParityError("\n".join(lines))

    logger.info(
        "batched mechanical parity gate complete: checks=%d mismatches=0 "
        "golden_exec=%.3fs candidate_exec=%.3fs golden_eval=%.3fs "
        "candidate_eval=%.3fs eval=%.3fs elapsed=%.3fs; next: disk flush / Pass 2",
        total_checks,
        golden_seconds,
        candidate_seconds,
        golden_eval_seconds,
        candidate_eval_seconds,
        eval_seconds,
        gate_elapsed,
    )


@lru_cache(maxsize=8)
def _dist_data(package_root: str) -> ModuleType:
    """Load the exported ``data.py`` (DEFAULT_INPUTS/CONSTANTS) in isolation."""
    data_path = Path(package_root) / "data.py"
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
    package_root: Path,
    constraints: Mapping[str, object],
    count: int = DEFAULT_SAMPLE_COUNT,
    seed: int = DEFAULT_SAMPLE_SEED,
) -> list[dict[str, object]]:
    """Build gate input vectors from pipeline constraints and exported defaults."""
    data = _dist_data(_resolved_package_root(package_root))
    return sample_input_vectors(
        constraints=constraints,
        default_inputs=data.DEFAULT_INPUTS,
        constants=data.CONSTANTS,
        count=count,
        seed=seed,
    )


def _numeric_constraint_probe_values(annotation: object) -> tuple[object, ...]:
    """Return min, max, and 0 when in range for numeric constraint annotations."""
    metadata = getattr(annotation, "__metadata__", None)
    if metadata is None:
        return ()
    for meta in metadata:
        if isinstance(meta, RealBetween):
            probes: list[object] = [meta.min, meta.max]
            if meta.min < 0.0 < meta.max:
                probes.append(0.0)
            return tuple(dict.fromkeys(probes))
        if isinstance(meta, Between):
            probes = [meta.min, meta.max]
            if meta.min <= 0 <= meta.max:
                probes.append(0)
            return tuple(dict.fromkeys(probes))
    return ()


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
    """Build input vectors: default, numeric boundary probes, then random draws.

    The first vector is ``default_inputs`` merged with ``constants``. For each
    default input with a numeric ``RealBetween`` or ``Between`` constraint, one
    vector is added per probe value (minimum, maximum, and 0 when in range)
    while other inputs stay at their defaults. Additional vectors keep constants
    fixed and perturb every default input within its declared constraint domain.
    Sampling is deterministic for a given seed so the gate is reproducible.
    """
    base = {**default_inputs, **constants}
    vectors: list[dict[str, object]] = [dict(base)]
    sampled_addresses = [
        address for address in default_inputs if address in constraints
    ]
    for address in sampled_addresses:
        for probe in _numeric_constraint_probe_values(constraints[address]):
            vector = dict(base)
            vector[address] = probe
            vectors.append(vector)
    rng = random.Random(seed)
    for _ in range(count):
        vector = dict(base)
        for address in sampled_addresses:
            vector[address] = _sample_constraint(
                constraints[address], default_inputs[address], rng
            )
        vectors.append(vector)
    return vectors
