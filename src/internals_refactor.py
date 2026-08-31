from __future__ import annotations

import ast
import builtins
import hashlib
import itertools
import json
import logging
import os
import re
import textwrap
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from dotenv import load_dotenv
from excel_grapher.exporter import ProjectionResult
from excel_grapher.grapher.graph import DependencyGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.async_gather import run_map_as_completed
from src.formula_clustering import FormulaCluster
from src.internal_bindings import InternalBindingIndex, internal_binding_for_address
from src.key_dispatch_synthesis import KeyDispatchPlan, plan_key_dispatch
from src.llm_json import (
    DEFAULT_MAX_ATTEMPTS,
    ValidatedJsonFailure,
    generate_validated_json,
    generate_validated_json_async,
)
from src.llm_providers import (
    build_async_client,
    build_client,
    get_llm_semaphore,
    model_from_env,
    provider_for_model,
)
from src.mechanical_body import MechanicalBodyDraft
from src.mechanical_naming import ClusterNamingLLMResponse
from src.peel_entrypoint_dispatch import inject_peel_entrypoint_dispatch
from src.pipeline_monitor import StageTimer
from src.refactor_bindings import (
    BindingKeyValue,
    KeyConceptSpec,
    dimension_id_to_param_name,
    expected_member_keys_for_cluster,
    format_binding_key_literal,
    helper_parameters_for_varying_keys,
    load_key_concept_vocabulary,
    render_literal_helper_call,
    resolve_dimension_key,
)
from src.refactor_contracts import (
    ClusterRefactorContract,
    concepts_with_multiple_dimensions,
    select_cluster_refactor_contract,
)
from src.refactor_fingerprints import (
    ClusterFingerprintSummary,
    SemanticDependencyRef,
    build_cluster_fingerprint_summary,
    format_cluster_fingerprint_dump,
)
from src.refactor_order import (
    RefactorUnit,
    compute_refactor_schedule,
    refactor_failure_target,
)
from src.refactor_return_types import (
    ALLOWED_REFACTOR_RETURN_TYPE_HINTS,
    KNOWN_RUNTIME_RETURN_HINTS,
    _binding_dtype_to_python,
    build_callee_return_hints,
    infer_refactor_return_type_hint,
    merge_callee_return_hints,
    merge_callee_return_hints_from_functions,
    normalize_return_type_hint_for_allowlist,
)
from src.runtime_symbols import (
    allowed_runtime_module_symbols,
    allowed_runtime_symbols,
    clear_runtime_symbol_caches,
)
from src.semantic_naming import (
    BindingRecordHints,
    _is_semantic_helper_def,
    allocate_schedule_helper_names,
    binding_record_hints_from_cell,
    cluster_binding_naming_hints,
    semantic_helpers_available_for_calls,
    sole_series_id_for_addresses,
    validate_semantic_identifier,
)
from src.workbook_addresses import parse_workbook_address

repo_root = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

REFACTOR_MODEL_ENV = "REFACTOR_MODEL"
REFACTOR_PROMPT_VERSION = 32
CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT = 30
_FINGERPRINT_FALLBACK_COUNT = 0
MECHANICAL_INTERNALS_CHECKPOINT_NAME = "internals.mechanical.py"
DEFAULT_INTERNALS_CACHE_DIR = repo_root / ".cache" / "internals"


def package_root_checkpoint_namespace(package_root: Path) -> str:
    """Stable directory name for a package root under ``.cache/internals/``."""
    return hashlib.sha256(str(package_root.resolve()).encode()).hexdigest()[:16]


def mechanical_internals_checkpoint_path(internals_path: Path) -> Path:
    """Pass 1 mechanical checkpoint under ``.cache/internals/``, per package root.

    Namespaced by the resolved package root so lab runs
    (``artifacts/refactor-lab``, ``artifacts/refactor-bucket-codegen``) do not
    collide with a real ``dist/<package>/`` run. The checkpoint is not a package
    module and must not live under ``dist/``.
    """
    package_root = internals_path.parent
    namespace = package_root_checkpoint_namespace(package_root)
    return (
        DEFAULT_INTERNALS_CACHE_DIR / namespace / MECHANICAL_INTERNALS_CHECKPOINT_NAME
    )


RefactorPromptObserver = Callable[[str, str, str], None]
"""Hook receiving ``(kind, target, prompt)`` for each refactor unit's user prompt."""

_PROMPT_OBSERVER: RefactorPromptObserver | None = None


def set_refactor_prompt_observer(observer: RefactorPromptObserver | None) -> None:
    """Install (or clear) a hook that sees every refactor prompt as it is built.

    The observer fires before the cache check, so prompts are observable even on
    fully cached runs. Used by ``scripts/run_refactor_stage.py --dump-prompts``.
    """
    global _PROMPT_OBSERVER
    _PROMPT_OBSERVER = observer


ClusterContextObserver = Callable[["ClusterRefactorContext"], None]
"""Hook receiving each cluster refactor context as its unit is processed."""

_CLUSTER_CONTEXT_OBSERVER: ClusterContextObserver | None = None


def set_cluster_context_observer(observer: ClusterContextObserver | None) -> None:
    """Install (or clear) a hook that sees every cluster refactor context.

    Fires before the cache check in ``llm_refactor_cluster`` so diagnostics like
    ``scripts/run_refactor_stage.py --report-synthesis`` observe the exact
    contexts (including mid-refactor semantic dependencies) of a real run.
    """
    global _CLUSTER_CONTEXT_OBSERVER
    _CLUSTER_CONTEXT_OBSERVER = observer


SingletonContextObserver = Callable[["SingletonRefactorContext"], None]
"""Hook receiving each singleton refactor context as its unit is processed."""

_SINGLETON_CONTEXT_OBSERVER: SingletonContextObserver | None = None


def set_singleton_context_observer(
    observer: SingletonContextObserver | None,
) -> None:
    """Install (or clear) a hook that sees every singleton refactor context.

    Fires before the cache check in ``llm_refactor_singleton`` so diagnostics
    like ``scripts/run_refactor_stage.py --report-synthesis`` observe the exact
    contexts (including mid-refactor semantic dependencies) of a real run.
    """
    global _SINGLETON_CONTEXT_OBSERVER
    _SINGLETON_CONTEXT_OBSERVER = observer


@dataclass(frozen=True)
class Pass1UnitTiming:
    """Wall-clock phase timings for one Pass 1 schedule unit."""

    unit_id: str
    kind: Literal["singleton", "cluster"]
    member_count: int
    context_s: float
    synthesize_s: float
    apply_s: float
    validate_s: float
    reindex_s: float
    reindexed: bool
    apply_batch_size: int
    dirty_count: int
    source_bytes: int
    mechanical: bool

    def as_log_fields(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "member_count": self.member_count,
            "context_s": self.context_s,
            "synthesize_s": self.synthesize_s,
            "apply_s": self.apply_s,
            "validate_s": self.validate_s,
            "reindex_s": self.reindex_s,
            "reindexed": self.reindexed,
            "apply_batch_size": self.apply_batch_size,
            "dirty_count": self.dirty_count,
            "source_bytes": self.source_bytes,
            "mechanical": self.mechanical,
        }


Pass1UnitTimingObserver = Callable[[Pass1UnitTiming], None]
"""Hook receiving each Pass 1 unit timing record after that unit's apply."""

_PASS1_UNIT_TIMING_OBSERVER: Pass1UnitTimingObserver | None = None


def set_pass1_unit_timing_observer(
    observer: Pass1UnitTimingObserver | None,
) -> None:
    """Install (or clear) a hook that receives per-unit Pass 1 phase timings.

    When set, timings are collected even if ``PASS1_UNIT_TIMERS`` is unset so
    tests and diagnostics can observe cadence without enabling log spam.
    """
    global _PASS1_UNIT_TIMING_OBSERVER
    _PASS1_UNIT_TIMING_OBSERVER = observer


def _pass1_unit_timers_enabled() -> bool:
    value = os.environ.get("PASS1_UNIT_TIMERS", "0").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def _pass1_unit_timers_jsonl_path() -> Path | None:
    raw = os.environ.get("PASS1_UNIT_TIMERS_JSONL", "").strip()
    return Path(raw) if raw else None


def _pass1_unit_timing_active() -> bool:
    return _PASS1_UNIT_TIMING_OBSERVER is not None or _pass1_unit_timers_enabled()


def _emit_pass1_unit_timing(timing: Pass1UnitTiming) -> None:
    if _PASS1_UNIT_TIMING_OBSERVER is not None:
        _PASS1_UNIT_TIMING_OBSERVER(timing)
    if not _pass1_unit_timers_enabled():
        return
    logger.info(
        "pass1 unit timing: target=%s kind=%s members=%d mechanical=%s "
        "context=%.3fs synthesize=%.3fs apply=%.3fs validate=%.3fs "
        "reindex=%.3fs reindexed=%s apply_batch_size=%d dirty=%d "
        "source_bytes=%d",
        timing.unit_id,
        timing.kind,
        timing.member_count,
        timing.mechanical,
        timing.context_s,
        timing.synthesize_s,
        timing.apply_s,
        timing.validate_s,
        timing.reindex_s,
        int(timing.reindexed),
        timing.apply_batch_size,
        timing.dirty_count,
        timing.source_bytes,
    )
    jsonl_path = _pass1_unit_timers_jsonl_path()
    if jsonl_path is None:
        return
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(timing.as_log_fields(), sort_keys=True) + "\n")


def _record_fingerprint_fallback(reason: str, *, cluster_id: int) -> None:
    global _FINGERPRINT_FALLBACK_COUNT
    _FINGERPRINT_FALLBACK_COUNT += 1
    logger.info(
        "cluster %s fingerprint dump fallback (%s); using legacy sampled sources "
        "(fallback_count=%s)",
        cluster_id,
        reason,
        _FINGERPRINT_FALLBACK_COUNT,
    )


def refactor_model() -> str:
    return model_from_env(REFACTOR_MODEL_ENV)


def _refactor_provider_key_present() -> bool:
    provider = provider_for_model(refactor_model())
    return bool(os.environ.get(provider.api_key_env))


REFACTOR_CACHE_PATH = repo_root / ".cache/internals-refactors.json"
REFACTOR_FAILURE_DUMP_DIR = repo_root / ".cache" / "refactor_failures"
FORMULA_SECTION_MARKER = "# --- Formula cell functions ---"
PROJECTION_ALIAS_SECTION_MARKER = "# --- Projection public address aliases ---"
UNREFACTORED_CELLS_SECTION_MARKER = "# --- Unrefactored formula cells ---"
RESOLVER_SECTION_MARKER = "# --- Formula resolver ---"

AddressDispatch = dict[str, tuple[str, dict[str, BindingKeyValue]]]

REFACTOR_ROW_ORDER: tuple[int, ...] = ()
"""Optional legacy row order hint; prefer ``compute_refactor_schedule``."""


def _refactor_failure_target_slug(
    *, kind: Literal["singleton", "cluster"], target: str
) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_")
    if kind == "cluster" and not slug.startswith("cluster_"):
        return f"cluster_{slug}"
    return slug or kind


_REFACTOR_FAILURE_COMPATIBILITY_NOTE = (
    "Top-level llm_response.json, prepared_response.json, and raw_content.json "
    "remain the last attempt for compatibility. Full retry history is in "
    "conversation.json and attempts/."
)


def _write_json_artifact(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def write_refactor_failure_diagnostic(
    *,
    kind: Literal["singleton", "cluster"],
    target: str,
    error: BaseException,
    dump_dir: Path | None = None,
    user_prompt: str | None = None,
    llm_response: Mapping[str, Any] | None = None,
    prepared_response: Mapping[str, Any] | None = None,
    raw_content: str | None = None,
    conversation: Sequence[Mapping[str, Any]] | None = None,
    attempts: Sequence[Mapping[str, Any]] | None = None,
    context: Mapping[str, Any] | None = None,
    source: Literal["llm", "cache", "mechanical"] = "llm",
    model: str | None = None,
) -> Path:
    """Persist refactor failure artifacts for offline diagnosis.

    When ``conversation`` / ``attempts`` are provided (multi-attempt LLM
    validation failures), the dump includes the full chat history and
    per-attempt artifacts under ``attempts/NN/``. Legacy top-level response
    files continue to mirror the final attempt.

    Pass-1 mechanical failures set ``source="mechanical"`` and typically
    include a ``context`` payload (member sources, draft body, addresses).
    """
    root = dump_dir if dump_dir is not None else REFACTOR_FAILURE_DUMP_DIR
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = _refactor_failure_target_slug(kind=kind, target=target)
    failure_dir = root / slug
    failure_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    if llm_response is not None:
        files["llm_response"] = "llm_response.json"
        _write_json_artifact(failure_dir / files["llm_response"], dict(llm_response))
    if prepared_response is not None:
        files["prepared_response"] = "prepared_response.json"
        _write_json_artifact(
            failure_dir / files["prepared_response"], dict(prepared_response)
        )
    if raw_content is not None:
        files["raw_content"] = "raw_content.json"
        _write_json_artifact(
            failure_dir / files["raw_content"], {"content": raw_content}
        )
    if user_prompt is not None:
        files["user_prompt"] = "user_prompt.md"
        (failure_dir / files["user_prompt"]).write_text(
            user_prompt,
            encoding="utf-8",
        )
    if context is not None:
        files["context"] = "context.json"
        _write_json_artifact(failure_dir / files["context"], dict(context))
    if conversation is not None:
        files["conversation"] = "conversation.json"
        _write_json_artifact(
            failure_dir / files["conversation"],
            [dict(message) for message in conversation],
        )
    if attempts is not None:
        files["attempts"] = "attempts"
        attempts_root = failure_dir / files["attempts"]
        attempts_root.mkdir(parents=True, exist_ok=True)
        for attempt in attempts:
            attempt_number = int(attempt["attempt"])
            attempt_dir = attempts_root / f"{attempt_number:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            (attempt_dir / "error.txt").write_text(
                str(attempt.get("error", "")) + "\n",
                encoding="utf-8",
            )
            raw = attempt.get("raw_content")
            if isinstance(raw, str):
                _write_json_artifact(attempt_dir / "raw_content.json", {"content": raw})
            llm_payload = attempt.get("llm_response")
            if isinstance(llm_payload, Mapping):
                _write_json_artifact(
                    attempt_dir / "llm_response.json", dict(llm_payload)
                )
            prepared_payload = attempt.get("prepared_response")
            if isinstance(prepared_payload, Mapping):
                _write_json_artifact(
                    attempt_dir / "prepared_response.json", dict(prepared_payload)
                )

    error_path = "error.txt"
    files["error"] = error_path
    (failure_dir / error_path).write_text(str(error) + "\n", encoding="utf-8")

    manifest: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "source": source,
        "model": model,
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "timestamp": timestamp,
        "files": files,
    }
    if conversation is not None or attempts is not None:
        manifest["schema_version"] = 2
        manifest["compatibility_note"] = _REFACTOR_FAILURE_COMPATIBILITY_NOTE
    (failure_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return failure_dir


def _mechanical_draft_context(draft: MechanicalBodyDraft) -> dict[str, Any]:
    return {
        "body": draft.body,
        "renameable_locals": list(draft.renameable_locals),
        "lookup_table_names": list(draft.lookup_table_names),
        "group_count": draft.group_count,
    }


def _mechanical_cluster_failure_context(
    ctx: ClusterRefactorContext,
    draft: MechanicalBodyDraft,
) -> dict[str, Any]:
    return {
        "cluster_id": ctx.cluster_id,
        "canonical_template": ctx.canonical_template,
        "expected_helper_name": ctx.expected_helper_name,
        "contract": ctx.contract,
        "naming_hints": dict(ctx.naming_hints),
        "members": [
            {
                "index": index,
                "address": member.address,
                "function_name": member.function_name,
                "python_source": member.python_source,
            }
            for index, member in enumerate(ctx.members)
        ],
        "mechanical_draft": _mechanical_draft_context(draft),
    }


def _mechanical_singleton_failure_context(
    ctx: SingletonRefactorContext,
    draft: MechanicalBodyDraft,
) -> dict[str, Any]:
    return {
        "address": ctx.address,
        "function_name": ctx.function_name,
        "canonical_template": ctx.canonical_template,
        "normalized_formula": ctx.normalized_formula,
        "python_source": ctx.python_source,
        "expected_helper_name": ctx.expected_helper_name,
        "naming_hints": dict(ctx.naming_hints),
        "mechanical_draft": _mechanical_draft_context(draft),
    }


def _response_dump(response: object) -> Mapping[str, Any] | None:
    model_dump = getattr(response, "model_dump", None)
    if not callable(model_dump):
        return None
    payload = model_dump()
    if isinstance(payload, Mapping):
        return payload
    return None


def _log_mechanical_pass1_failure(
    *,
    kind: Literal["singleton", "cluster"],
    target: str,
    error: BaseException,
    prepared_response: Mapping[str, Any] | None,
    context: Callable[[], Mapping[str, Any]],
    log_message: str,
    log_args: Sequence[object],
) -> None:
    """Best-effort dump + log for pass-1 mechanical failures.

    Dump construction must never mask the original exception.
    """
    try:
        dump_dir = write_refactor_failure_diagnostic(
            kind=kind,
            target=target,
            error=error,
            prepared_response=prepared_response,
            context=context(),
            source="mechanical",
        )
    except Exception as dump_error:  # noqa: BLE001
        logger.error(
            "failed to write mechanical refactor diagnostic kind=%s target=%s: %s",
            kind,
            target,
            dump_error,
        )
        logger.error(log_message, *log_args, "<unavailable>", error)
        return
    logger.error(log_message, *log_args, dump_dir, error)


def _artifact_matches_raw_content(
    artifact: Mapping[str, Any], raw_content: str
) -> bool:
    """Return whether ``artifact['llm_response']`` came from ``raw_content``.

    Raw JSON may omit null-valued fields that appear in ``model_dump()``. Every
    key present in the raw object must agree with the dump, and every non-null
    dump field must appear in the raw object so unrelated or partial payloads
    cannot claim a richer prepared/llm artifact.
    """
    llm_response = artifact.get("llm_response")
    if not isinstance(llm_response, Mapping):
        return False
    try:
        parsed_raw = json.loads(raw_content)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed_raw, dict) or not parsed_raw:
        return False
    for key, value in parsed_raw.items():
        if key not in llm_response or llm_response[key] != value:
            return False
    for key, value in llm_response.items():
        if value is None:
            continue
        if key not in parsed_raw:
            return False
    return True


def _attempt_artifacts_from_validated_json_failure(
    error: ValidatedJsonFailure,
    *,
    local_artifacts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge LLM retry records with per-attempt prepared/llm payloads."""
    unused = list(local_artifacts)
    merged: list[dict[str, Any]] = []
    for record in error.attempts:
        entry: dict[str, Any] = {
            "attempt": record.attempt,
            "raw_content": record.raw_content,
            "error": record.error,
        }
        for index, artifact in enumerate(unused):
            if not _artifact_matches_raw_content(artifact, record.raw_content):
                continue
            llm_payload = artifact.get("llm_response")
            if llm_payload is not None:
                entry["llm_response"] = llm_payload
            prepared_payload = artifact.get("prepared_response")
            if prepared_payload is not None:
                entry["prepared_response"] = prepared_payload
            del unused[index]
            break
        merged.append(entry)
    return merged


def _dump_validated_json_failure(
    *,
    kind: Literal["singleton", "cluster"],
    target: str,
    error: ValidatedJsonFailure,
    user_prompt: str,
    local_artifacts: Sequence[Mapping[str, Any]],
    model: str,
    dump_dir: Path | None = None,
) -> Path:
    attempts = _attempt_artifacts_from_validated_json_failure(
        error, local_artifacts=local_artifacts
    )
    last_attempt = attempts[-1] if attempts else {}
    llm_response = last_attempt.get("llm_response")
    prepared_response = last_attempt.get("prepared_response")
    raw_content = last_attempt.get("raw_content")
    return write_refactor_failure_diagnostic(
        kind=kind,
        target=target,
        error=error,
        dump_dir=dump_dir,
        user_prompt=user_prompt,
        llm_response=llm_response if isinstance(llm_response, Mapping) else None,
        prepared_response=(
            prepared_response if isinstance(prepared_response, Mapping) else None
        ),
        raw_content=raw_content if isinstance(raw_content, str) else None,
        conversation=error.messages,
        attempts=attempts,
        source="llm",
        model=model,
    )


@dataclass(frozen=True)
class CallSite:
    caller_function: str
    caller_address: str | None
    callee_function: str
    callee_address: str
    pattern: Literal["xl_eval", "direct"]
    line: int
    snippet: str


@dataclass(frozen=True)
class MemberContext:
    address: str
    function_name: str
    engine_column: str
    """Column letter of ``address``."""
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    dependency_functions: tuple[str, ...]
    binding_keys: dict[str, BindingKeyValue] | None = None
    binding_record: dict[str, BindingKeyValue] | None = None


@dataclass(frozen=True)
class SemanticDependency:
    """Maps a refactored upstream cell range to the semantic helper that covers it."""

    helper_name: str
    call_form: str
    address_template: str
    columns: tuple[str, ...]
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class ClusterRefactorContext:
    cluster_id: int
    canonical_template: str
    row: int | None
    members: tuple[MemberContext, ...]
    external_dependencies: tuple[str, ...]
    semantic_dependencies: tuple[SemanticDependency, ...]
    call_sites: tuple[CallSite, ...]
    first_year_column: str
    allowed_runtime_symbols: tuple[str, ...]
    key_vocabulary: tuple[KeyConceptSpec, ...]
    expected_member_keys: dict[str, dict[str, BindingKeyValue]]
    naming_hints: dict[str, object]
    expected_helper_name: str
    contract: ClusterRefactorContract = "member_sweep"
    fingerprint_summary: ClusterFingerprintSummary | None = None
    key_dispatch_plan: KeyDispatchPlan | None = None
    """Multi-regime series plan used by the ``key_dispatch`` contract."""
    key_dispatch_bound_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None = None
    """Bound-address keys used to synthesize each regime body."""
    package_root: Path | None = None


class HelperParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Python parameter name for the helper, e.g. time_period."
    )
    dimension_id: str = Field(
        description=(
            "Effective binding dimension id this parameter varies along, "
            "e.g. PROJECTION_PERIOD or TIME_PERIOD."
        )
    )
    dtype: str = Field(description="Expected Python dtype for the parameter.")
    concept: str | None = Field(
        default=None,
        description=(
            "SDMX-style concept referenced by the dimension, e.g. TIME_PERIOD. "
            "Optional; filled from key_vocabulary when omitted."
        ),
    )


class MemberKeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: str = Field(
        description=(
            "Effective binding dimension id, e.g. PROJECTION_PERIOD or TIME_PERIOD."
        )
    )
    value: str | int | float | bool = Field(
        description="Literal binding key value for this dimension."
    )


class MemberKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(description="Workbook address this entry covers.")
    function_name: str = Field(description="Existing cell_* function being replaced.")
    keys: tuple[MemberKeyEntry, ...] = Field(
        description=(
            "Literal binding key values for this address; one entry per dimension, "
            "excluding series-constant dimensions."
        )
    )

    def keys_dict(self) -> dict[str, BindingKeyValue]:
        return {entry.dimension_id: entry.value for entry in self.keys}


class ClusterRefactorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    helper_name: str = Field(
        description="New snake_case function in internals.py, e.g. shock_active."
    )
    helper_docstring: str = Field(
        description=(
            "Google-style docstring derived from the docstring embedded in "
            "helper_source. Include Args and Returns sections."
        )
    )
    parameters: tuple[HelperParameter, ...] = Field(
        description=(
            "Economic parameters the helper varies along, tied to binding "
            "dimension ids."
        )
    )
    helper_source: str = Field(
        description=(
            "Complete Python function definition including def line, Google-style "
            "docstring, and body. Signature must be (ctx, <parameters>). "
            "Use only runtime symbols from allowed_runtime_symbols."
        )
    )
    member_keys: tuple[MemberKeys, ...] = Field(
        description=(
            "One entry per cluster member with literal key values for that address."
        )
    )


class RefactorDeclaredError(RuntimeError):
    """Raised when the LLM declares that a refactor cannot proceed safely."""

    def __init__(
        self,
        reason: str,
        *,
        kind: Literal["singleton", "cluster"],
        target: str,
    ) -> None:
        self.reason = reason
        self.kind = kind
        self.target = target
        super().__init__(reason)


class _LlmErrorCapable(Protocol):
    """LLM response models that expose the shared error / error_reason fields."""

    error: bool | None
    error_reason: str | None


def _validate_llm_response_error_or_success[T: _LlmErrorCapable](
    response: T,
    *,
    success_fields: tuple[str, ...],
    optional_ignored_fields: tuple[str, ...] = (),
) -> T:
    error = response.error
    error_reason = response.error_reason
    if error is True:
        reason = error_reason.strip() if isinstance(error_reason, str) else ""
        if not reason:
            raise ValueError(
                "error_reason must be a non-empty string when error is true"
            )
        populated = [
            name for name in success_fields if getattr(response, name) is not None
        ]
        populated.extend(
            name
            for name in optional_ignored_fields
            if getattr(response, name, None) is not None
        )
        if populated:
            raise ValueError(
                "success fields must be null when error is true: "
                + ", ".join(populated)
            )
        return response
    if error_reason is not None:
        raise ValueError("error_reason must be null unless error is true")
    missing = [
        name
        for name in success_fields
        if getattr(response, name) is None
        or (
            isinstance(getattr(response, name), str)
            and not getattr(response, name).strip()
        )
    ]
    if missing:
        raise ValueError(
            "missing required fields for successful refactor: " + ", ".join(missing)
        )
    return response


def raise_if_llm_declared_error(
    response: _LlmErrorCapable,
    *,
    kind: Literal["singleton", "cluster"],
    target: str,
) -> None:
    """Abort immediately when the LLM sets ``error`` to true."""
    if response.error is not True:
        return
    error_reason = response.error_reason
    reason = error_reason.strip() if isinstance(error_reason, str) else ""
    if not reason:
        raise ValueError("error_reason must be a non-empty string when error is true")
    raise RefactorDeclaredError(reason, kind=kind, target=target)


class ClusterRefactorLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_docstring: str | None = Field(
        description=(
            "Google-style docstring. Include Args and Returns sections. "
            "Null when error is true."
        ),
    )
    symbol_body: str | None = Field(
        description="Python function body. Null when error is true.",
    )
    parameters: tuple[HelperParameter, ...] | None = Field(
        default=None,
        description=(
            "Deprecated: ignored when present. The pipeline synthesizes parameters "
            "from varying binding dimensions and key_vocabulary. Null when error "
            "is true or omitted on success."
        ),
    )
    member_keys: tuple[MemberKeys, ...] | None = Field(
        default=None,
        description=(
            "Deprecated: ignored when present. The pipeline synthesizes member_keys "
            "from expected binding keys. Null when error is true or omitted on success."
        ),
    )
    error: bool | None = Field(
        description=(
            "Set to true to abort this refactor and stop the pipeline when the "
            "cluster cannot be safely refactored. Null or false on success."
        ),
    )
    error_reason: str | None = Field(
        description=(
            "Human-readable explanation of why refactoring must abort. "
            "Non-empty when error is true; null otherwise."
        ),
    )

    @model_validator(mode="after")
    def _require_success_fields_or_declared_error(self) -> ClusterRefactorLLMResponse:
        return _validate_llm_response_error_or_success(
            self,
            success_fields=(
                "symbol_docstring",
                "symbol_body",
            ),
            optional_ignored_fields=("parameters", "member_keys"),
        )


CLUSTER_REFACTOR_PROMPT_FIXTURES: dict[ClusterRefactorContract, Path] = {
    "member_sweep": repo_root / "tests" / "fixtures" / "cluster_refactor_prompt.md",
    "dimension_aware": (
        repo_root / "tests" / "fixtures" / "cluster_refactor_prompt_dimension_aware.md"
    ),
    "key_dispatch": repo_root / "tests" / "fixtures" / "cluster_refactor_prompt.md",
}
CLUSTER_REFACTOR_PROMPT_FIXTURE = CLUSTER_REFACTOR_PROMPT_FIXTURES["member_sweep"]


@dataclass(frozen=True)
class SingletonRefactorContext:
    address: str
    function_name: str
    canonical_template: str
    normalized_formula: str
    python_source: str
    dependency_addresses: tuple[str, ...]
    external_dependencies: tuple[str, ...]
    semantic_dependencies: tuple[SemanticDependency, ...]
    call_sites: tuple[CallSite, ...]
    allowed_runtime_symbols: tuple[str, ...]
    naming_hints: dict[str, object]
    expected_helper_name: str
    inline_replacements: tuple[tuple[str, str], ...] = ()
    """``(cell_function_name, replacement_call_source)`` pairs for every
    dependency call site that resolves to a provably inlinable thin wrapper."""
    package_root: Path | None = None


class SingletonRefactorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_name: str = Field(
        description="Semantic snake_case function name, e.g. initial_debt_to_gdp."
    )
    symbol_docstring: str = Field(
        description=(
            "Google-style docstring derived from the docstring embedded in "
            "symbol_source. Include Args and Returns sections."
        )
    )
    symbol_source: str = Field(
        description=(
            "Complete Python function definition including def line, Google-style "
            "docstring, and body. Signature must be (ctx)."
        )
    )


class SingletonRefactorLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_docstring: str | None = Field(
        description=(
            "Google-style docstring. Include Args and Returns sections. "
            "Null when error is true."
        ),
    )
    symbol_body: str | None = Field(
        description="Python function body. Null when error is true.",
    )
    error: bool | None = Field(
        description=(
            "Set to true to abort this refactor and stop the pipeline when the "
            "cell cannot be safely refactored. Null or false on success."
        ),
    )
    error_reason: str | None = Field(
        description=(
            "Human-readable explanation of why refactoring must abort. "
            "Non-empty when error is true; null otherwise."
        ),
    )

    @model_validator(mode="after")
    def _require_success_fields_or_declared_error(self) -> SingletonRefactorLLMResponse:
        return _validate_llm_response_error_or_success(
            self,
            success_fields=(
                "symbol_docstring",
                "symbol_body",
            ),
        )


ALLOWED_SINGLETON_RETURN_TYPE_HINTS = ALLOWED_REFACTOR_RETURN_TYPE_HINTS
ALLOWED_REFACTOR_TYPE_HINT_NAMES = frozenset({"CellValue", "EvalContext"})

SINGLETON_REFACTOR_PROMPT_FIXTURE = (
    repo_root / "tests" / "fixtures" / "singleton_refactor_prompt.md"
)


@dataclass(frozen=True)
class CollapseBinding:
    address: str
    function_name: str
    helper_name: str
    literal_call: str


@dataclass(frozen=True)
class ClusterRefactorApplyResult:
    source: str
    helper_name: str
    wrappers_applied: tuple[str, ...]
    dry_run: bool
    response: ClusterRefactorResponse
    phase_c_pruned: int = 0


@dataclass(frozen=True)
class InternalsRefactorRunResult:
    """Outcome of :func:`refactor_internals_all_clusters` for cache decisions."""

    apply_results: tuple[ClusterRefactorApplyResult, ...]
    final_source: str
    cacheable: bool

    def __iter__(self):
        return iter(self.apply_results)

    def __len__(self) -> int:
        return len(self.apply_results)


@dataclass(frozen=True)
class SingletonRefactorApplyResult:
    source: str
    symbol_name: str
    old_function_name: str
    reference_rewrites: int
    dry_run: bool
    response: SingletonRefactorResponse


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def address_to_function_name(address: str) -> str:
    name: list[str] = []
    prev_underscore = False
    for character in address.lower():
        if character == "'":
            continue
        if "a" <= character <= "z" or "0" <= character <= "9":
            name.append(character)
            prev_underscore = False
        elif not prev_underscore:
            name.append("_")
            prev_underscore = True
    base = "".join(name).strip("_")
    return f"cell_{base}"


def _member_engine_column(address: str) -> str:
    _, column, _row = parse_workbook_address(address)
    return column


def _binding_hints_for_address(
    internal_binding_index: InternalBindingIndex | None,
    address: str,
) -> BindingRecordHints:
    if internal_binding_index is None:
        return BindingRecordHints()
    binding = internal_binding_for_address(internal_binding_index, address)
    if binding is None:
        return BindingRecordHints()
    return binding_record_hints_from_cell(
        {
            "key": binding.key,
            "record": binding.record,
        }
    )


def _default_key_vocabulary(bindings_path: Path) -> tuple[KeyConceptSpec, ...]:
    return load_key_concept_vocabulary(bindings_path)


def build_cluster_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
    *,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None = None,
    key_vocabulary: tuple[KeyConceptSpec, ...] | None = None,
    workbook_path: Path,
    bindings_path: Path,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    internals_index: InternalsSourceIndex | None = None,
    address_to_series_id: Mapping[str, str] | None = None,
    address_to_helper_name: Mapping[str, str] | None = None,
    expected_helper_name: str | None = None,
    existing_helper_names: frozenset[str] | None = None,
) -> ClusterRefactorContext | None:
    if len(cluster.members) < 2:
        return None

    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    defined_functions = index.functions

    member_addresses = frozenset(cluster.members)
    member_functions = {
        address_to_function_name(address) for address in member_addresses
    }

    members: list[MemberContext] = []
    for address in cluster.members:
        function_name = address_to_function_name(address)
        if function_name not in defined_functions:
            continue

        engine_column = _member_engine_column(address)

        node = projection.get_node(address)
        if node is None or node.normalized_formula is None:
            continue

        dependency_addresses = tuple(sorted(projection.get_dependencies(address)))
        cluster_address_set = set(cluster.members)
        dependency_functions = tuple(
            sorted(
                address_to_function_name(dependency)
                for dependency in dependency_addresses
                if dependency in cluster_address_set
            )
        )

        binding_hints = _binding_hints_for_address(internal_binding_index, address)

        members.append(
            MemberContext(
                address=address,
                function_name=function_name,
                engine_column=engine_column,
                normalized_formula=node.normalized_formula,
                python_source=index.function_source(function_name),
                dependency_addresses=dependency_addresses,
                dependency_functions=dependency_functions,
                binding_keys=binding_hints.binding_keys,
                binding_record=binding_hints.binding_record,
            )
        )

    if len(members) < 2:
        return None

    if bound_address_keys is None:
        raise ValueError(
            "bound_address_keys is required; pass keys from the extract stage "
            "or series-derived cache rather than relying on ambient config"
        )
    resolved_bound_keys = bound_address_keys
    resolved_vocabulary = (
        key_vocabulary
        if key_vocabulary is not None
        else _default_key_vocabulary(bindings_path)
    )
    member_address_list = tuple(member.address for member in members)
    expected_member_keys = expected_member_keys_for_cluster(
        member_address_list,
        bound_address_keys=resolved_bound_keys,
    )
    varying_dimension_ids = frozenset(
        dimension_id for keys in expected_member_keys.values() for dimension_id in keys
    )
    formula_nodes = {member.address: member.normalized_formula for member in members}
    active_cluster = replace(cluster, members=member_address_list)
    contract = select_cluster_refactor_contract(
        active_cluster,
        formula_nodes,
        resolved_bound_keys,
        varying_dimension_ids,
        key_vocabulary=resolved_vocabulary,
        workbook_path=workbook_path,
    )
    key_dispatch_plan: KeyDispatchPlan | None = None
    gate_rejected_operand_variation = False
    if contract is None:
        planned_helper_name = expected_helper_name
        if planned_helper_name is None:
            if address_to_series_id is None:
                raise ValueError(
                    "address_to_series_id is required to lock cluster helper names"
                )
            planned_helper_name = sole_series_id_for_addresses(
                member_address_list,
                address_to_series_id,
            )
        key_dispatch_plan = plan_key_dispatch(
            active_cluster,
            formula_nodes,
            expected_member_keys,
            helper_name=planned_helper_name,
        )
        if key_dispatch_plan is None:
            # #132: the operand-level routing gate predates the current
            # mechanical-synthesis coverage (offsets/lags/lookups with per-member
            # verification). Rather than skip outright, fall through as
            # member_sweep and let verified synthesis be the arbiter (see the
            # probe below). Genuinely unroutable clusters still fail synthesis and
            # keep today's skip, so this only rescues clusters mechanical
            # synthesis can actually reproduce -- no new LLM fallbacks.
            gate_rejected_operand_variation = True
            contract = "member_sweep"
        else:
            contract = "key_dispatch"
            logger.info(
                "cluster %s rescued as key_dispatch on %s (%d regimes)",
                cluster.cluster_id,
                key_dispatch_plan.dispatch_dimension_id,
                len(key_dispatch_plan.regimes),
            )

    external_dependency_addresses = sorted(
        {
            dependency
            for member in members
            for dependency in member.dependency_addresses
            if dependency not in member_addresses
        }
    )
    semantic_dependencies, unresolved = resolve_semantic_dependencies(
        source,
        external_dependency_addresses,
        index=index,
        address_to_series_id=address_to_series_id,
        address_to_helper_name=address_to_helper_name,
        bound_address_keys=resolved_bound_keys,
    )
    external_dependencies = tuple(
        sorted(
            {dependency.helper_name for dependency in semantic_dependencies}
            | set(unresolved)
        )
    )

    semantic_refs = tuple(
        SemanticDependencyRef(
            helper_name=dependency.helper_name,
            call_form=dependency.call_form,
            address_template=dependency.address_template,
            addresses=dependency.addresses,
        )
        for dependency in semantic_dependencies
    )
    fingerprint_summary = build_cluster_fingerprint_summary(
        members,
        expected_member_keys=expected_member_keys,
        bound_address_keys=resolved_bound_keys,
        workbook_path=workbook_path,
        address_to_series_id=address_to_series_id,
        semantic_dependencies=semantic_refs,
    )
    if fingerprint_summary.fallback_reason is not None:
        _record_fingerprint_fallback(
            fingerprint_summary.fallback_reason,
            cluster_id=cluster.cluster_id,
        )

    if expected_helper_name is None:
        if address_to_series_id is None:
            raise ValueError(
                "address_to_series_id is required to lock cluster helper names"
            )
        expected_helper_name = sole_series_id_for_addresses(
            member_address_list,
            address_to_series_id,
        )
    reserved_names = (
        existing_helper_names if existing_helper_names is not None else frozenset()
    )
    validate_semantic_identifier(
        expected_helper_name,
        existing_names=reserved_names - {expected_helper_name},
    )

    if gate_rejected_operand_variation:
        # Probe: keep this rescued cluster only if verified mechanical synthesis
        # can reproduce it. Otherwise restore the routing gate's skip (#132).
        from src.mechanical_body import (
            MechanicalSynthesisError,
            synthesize_cluster_body,
        )

        skip_reason: str | None = None
        if fingerprint_summary.fallback_reason is not None:
            skip_reason = f"fingerprint_fallback:{fingerprint_summary.fallback_reason}"
        else:
            try:
                synthesize_cluster_body(
                    fingerprint_summary,
                    key_vocabulary=resolved_vocabulary,
                    expected_member_keys=expected_member_keys,
                    helper_name=expected_helper_name,
                )
            except MechanicalSynthesisError as error:
                skip_reason = error.reason
        if skip_reason is not None:
            logger.info(
                "cluster %s skipped: operand-level variation not routable and "
                "mechanical synthesis unavailable (%s)",
                cluster.cluster_id,
                skip_reason,
            )
            return None
        logger.info(
            "cluster %s rescued to member_sweep: verified mechanical synthesis "
            "covers operand-level variation the routing gate rejected",
            cluster.cluster_id,
        )

    package_root = internals_path.parent
    return ClusterRefactorContext(
        cluster_id=cluster.cluster_id,
        canonical_template=cluster.canonical_template,
        row=cluster.row,
        members=tuple(members),
        external_dependencies=external_dependencies,
        semantic_dependencies=semantic_dependencies,
        call_sites=scan_call_sites(
            source, member_addresses, member_functions, index=index
        ),
        first_year_column=members[0].engine_column,
        allowed_runtime_symbols=allowed_runtime_symbols(package_root),
        key_vocabulary=resolved_vocabulary,
        expected_member_keys=expected_member_keys,
        naming_hints=cluster_binding_naming_hints(
            tuple(
                BindingRecordHints(
                    binding_keys=member.binding_keys,
                    binding_record=member.binding_record,
                )
                for member in members
            )
        ),
        expected_helper_name=expected_helper_name,
        contract=contract,
        fingerprint_summary=fingerprint_summary,
        key_dispatch_plan=key_dispatch_plan,
        key_dispatch_bound_keys=(
            dict(resolved_bound_keys) if key_dispatch_plan is not None else None
        ),
        package_root=package_root,
    )


def _singleton_inline_replacements(
    python_source: str,
    dependency_addresses: tuple[str, ...],
    index: InternalsSourceIndex,
) -> tuple[tuple[str, str], ...]:
    """Map each inlinable ``cell_*`` dependency call to its wrapper's call form.

    Only wrappers for recorded dependency addresses qualify, so the mechanical
    singleton body can never rewire a read the graph does not know about.
    """
    from src.mechanical_body import parse_inlinable_wrapper

    dependency_functions = {
        address_to_function_name(address) for address in dependency_addresses
    }
    replacements: list[tuple[str, str]] = []
    for name in _called_function_names(python_source):
        if not name.startswith("cell_") or name not in dependency_functions:
            continue
        node = index.functions.get(name)
        if node is None:
            continue
        replacement = parse_inlinable_wrapper(node)
        if replacement is not None:
            replacements.append((name, replacement))
    return tuple(replacements)


def build_singleton_refactor_context(
    projection: ProjectionResult,
    cluster: FormulaCluster,
    internals_path: Path,
    *,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    internals_index: InternalsSourceIndex | None = None,
    address_to_series_id: Mapping[str, str] | None = None,
    address_to_helper_name: Mapping[str, str] | None = None,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None = None,
    expected_helper_name: str | None = None,
    existing_helper_names: frozenset[str] | None = None,
) -> SingletonRefactorContext | None:
    if len(cluster.members) != 1:
        return None

    address = cluster.members[0]
    package_root = internals_path.parent

    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    defined_functions = index.functions

    function_name = address_to_function_name(address)
    if function_name not in defined_functions:
        return None

    node = projection.get_node(address)
    if node is None or node.normalized_formula is None:
        return None

    dependency_addresses = tuple(sorted(projection.get_dependencies(address)))
    semantic_dependencies, unresolved = resolve_semantic_dependencies(
        source,
        dependency_addresses,
        index=index,
        address_to_series_id=address_to_series_id,
        address_to_helper_name=address_to_helper_name,
        bound_address_keys=bound_address_keys,
    )
    external_dependencies = tuple(
        sorted(
            {dependency.helper_name for dependency in semantic_dependencies}
            | set(unresolved)
        )
    )

    if expected_helper_name is None:
        if address_to_series_id is None:
            raise ValueError(
                "address_to_series_id is required to lock singleton helper names"
            )
        expected_helper_name = sole_series_id_for_addresses(
            (address,),
            address_to_series_id,
        )
    reserved_names = (
        existing_helper_names if existing_helper_names is not None else frozenset()
    )
    validate_semantic_identifier(
        expected_helper_name,
        existing_names=reserved_names - {expected_helper_name},
    )

    python_source = index.function_source(function_name)
    return SingletonRefactorContext(
        address=address,
        function_name=function_name,
        canonical_template=cluster.canonical_template,
        normalized_formula=node.normalized_formula,
        python_source=python_source,
        dependency_addresses=dependency_addresses,
        external_dependencies=external_dependencies,
        semantic_dependencies=semantic_dependencies,
        call_sites=scan_call_sites(
            source,
            frozenset({address}),
            {function_name},
            index=index,
        ),
        allowed_runtime_symbols=allowed_runtime_symbols(package_root),
        naming_hints=_binding_hints_for_address(
            internal_binding_index, address
        ).to_payload(),
        expected_helper_name=expected_helper_name,
        inline_replacements=_singleton_inline_replacements(
            python_source, dependency_addresses, index
        ),
        package_root=package_root,
    )


@dataclass(frozen=True)
class InternalsSourceIndex:
    """Parse-once view of ``internals.py`` for refactor context construction."""

    source: str
    module: ast.Module
    lines_keepends: tuple[str, ...]
    lines: tuple[str, ...]
    functions: Mapping[str, ast.FunctionDef]
    semantic_helper_names: frozenset[str]
    address_dispatch: AddressDispatch
    symbol_dispatch: Mapping[str, str]

    @classmethod
    def from_source(cls, source: str) -> InternalsSourceIndex:
        module = ast.parse(source)
        functions = {
            node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
        }
        return cls(
            source=source,
            module=module,
            lines_keepends=tuple(source.splitlines(keepends=True)),
            lines=tuple(source.splitlines()),
            functions=functions,
            semantic_helper_names=frozenset(
                name
                for name, node in functions.items()
                if _is_semantic_helper_def(node)
            ),
            address_dispatch=_parse_address_dispatch(source, module=module) or {},
            symbol_dispatch=_parse_symbol_dispatch(source, module=module),
        )

    def function_source(self, function_name: str) -> str:
        node = self.functions.get(function_name)
        if node is None:
            raise KeyError(f"Function {function_name!r} not found in internals source")
        return "".join(self.lines_keepends[node.lineno - 1 : node.end_lineno])


def extract_function_source(source: str, function_name: str) -> str:
    return InternalsSourceIndex.from_source(source).function_source(function_name)


def _resolve_internals_index(
    internals_path: Path,
    *,
    internals_index: InternalsSourceIndex | None = None,
) -> InternalsSourceIndex:
    if internals_index is not None:
        return internals_index
    return InternalsSourceIndex.from_source(internals_path.read_text(encoding="utf-8"))


def scan_call_sites(
    source: str,
    member_addresses: frozenset[str],
    member_functions: set[str],
    *,
    index: InternalsSourceIndex | None = None,
) -> tuple[CallSite, ...]:
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    sites: list[CallSite] = []
    lines = list(resolved.lines)

    for top_level in resolved.module.body:
        if not isinstance(top_level, ast.FunctionDef):
            continue
        caller_function = top_level.name
        caller_address = _caller_address(caller_function)
        visitor = _CallSiteVisitor(
            caller_function=caller_function,
            caller_address=caller_address,
            member_addresses=member_addresses,
            member_functions=member_functions,
            lines=lines,
            sites=sites,
        )
        visitor.visit(top_level)

    return tuple(sorted(sites, key=lambda site: (site.line, site.callee_address)))


def _normalize_internals_bytes(internals_bytes: bytes) -> bytes:
    """Normalize line endings so cache keys are platform-independent."""
    return internals_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def refactor_cache_key(
    ctx: ClusterRefactorContext,
    internals_bytes: bytes,
    response_schema: dict[str, object],
) -> str:
    payload = {
        "model": refactor_model(),
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "contract": ctx.contract,
        "cluster_id": ctx.cluster_id,
        "canonical_template": ctx.canonical_template,
        "members": [
            {
                "address": member.address,
                "function_name": member.function_name,
                "formula_sha256": hashlib.sha256(
                    member.normalized_formula.encode()
                ).hexdigest(),
                "source_sha256": hashlib.sha256(
                    member.python_source.encode()
                ).hexdigest(),
            }
            for member in ctx.members
        ],
        "internals_sha256": hashlib.sha256(
            _normalize_internals_bytes(internals_bytes)
        ).hexdigest(),
        "response_schema_sha256": hashlib.sha256(
            stable_json(response_schema).encode()
        ).hexdigest(),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def prompt_payload(ctx: ClusterRefactorContext) -> dict[str, object]:
    varying_dimension_ids = frozenset(
        dimension_id
        for keys in ctx.expected_member_keys.values()
        for dimension_id in keys
    )
    parameter_names = [
        item.suggested_param_name
        for item in ctx.key_vocabulary
        if item.dimension_id in varying_dimension_ids
    ]
    return {
        "cluster_id": ctx.cluster_id,
        "row": ctx.row,
        "canonical_template": ctx.canonical_template,
        "first_year_column": ctx.first_year_column,
        "key_vocabulary": [
            {
                "dimension_id": item.dimension_id,
                "concept": item.concept,
                "dtype": item.dtype,
                "suggested_param_name": item.suggested_param_name,
            }
            for item in ctx.key_vocabulary
        ],
        "members": [
            {
                "address": member.address,
                "function_name": member.function_name,
                "engine_column": member.engine_column,
                "expected_keys": ctx.expected_member_keys.get(member.address, {}),
                "normalized_formula": member.normalized_formula,
                "python_source": member.python_source,
                "dependency_addresses": member.dependency_addresses,
                "dependency_functions": member.dependency_functions,
                "binding_keys": member.binding_keys or {},
                "binding_record": member.binding_record or {},
            }
            for member in ctx.members
        ],
        "external_dependencies": list(ctx.external_dependencies),
        "semantic_dependencies": [
            {
                "address_template": dependency.address_template,
                "columns": list(dependency.columns),
                "helper_name": dependency.helper_name,
                "call_form": dependency.call_form,
            }
            for dependency in ctx.semantic_dependencies
        ],
        "call_sites": [
            {
                "caller_function": site.caller_function,
                "caller_address": site.caller_address,
                "callee_function": site.callee_function,
                "callee_address": site.callee_address,
                "pattern": site.pattern,
                "line": site.line,
                "snippet": site.snippet,
            }
            for site in ctx.call_sites
        ],
        "constraints": {
            "allowed_runtime_symbols": list(ctx.allowed_runtime_symbols),
            "parameters": parameter_names,
            "signature": f"(ctx, {', '.join(parameter_names)})",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
            "docstring_style": "google",
            "require_semantic_locals": True,
        },
        "naming_hints": ctx.naming_hints,
        "expected_helper_name": ctx.expected_helper_name,
    }


def load_refactor_cache() -> dict[str, str]:
    if not REFACTOR_CACHE_PATH.exists():
        return {}
    return json.loads(REFACTOR_CACHE_PATH.read_text(encoding="utf-8"))


def save_refactor_cache(cache: dict[str, str]) -> None:
    REFACTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REFACTOR_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def validate_google_style_docstring(docstring: str) -> None:
    text = docstring.strip()
    if not text:
        raise ValueError("helper docstring must be non-empty")
    if "Args:" not in text:
        raise ValueError("helper docstring must include an Args section")
    if "Returns:" not in text:
        raise ValueError("helper docstring must include a Returns section")
    lines = text.splitlines()
    if not lines[0].strip():
        raise ValueError("helper docstring must begin with a one-line summary")
    remainder_start = 1
    while remainder_start < len(lines) and not lines[remainder_start].strip():
        remainder_start += 1
    if remainder_start < len(lines):
        first_rest = lines[remainder_start].strip()
        if not first_rest.startswith(("Args:", "Returns:", "Note:")):
            raise ValueError(
                "helper docstring summary must be followed by Args, Returns, or Note"
            )


_GOOGLE_DOCSTRING_SECTION_HEADER = re.compile(r"^(Args:|Returns:|Note:)\s*$")


def _normalize_google_docstring(docstring: str) -> str:
    text = textwrap.dedent(docstring).strip()
    if not text:
        return text
    lines = text.splitlines()
    summary = lines[0].strip()
    first_section_idx: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if _GOOGLE_DOCSTRING_SECTION_HEADER.match(line.strip()):
            first_section_idx = index
            break
    if first_section_idx is None:
        return text
    normalized_lines = [summary, "", *lines[first_section_idx:]]
    return "\n".join(normalized_lines).rstrip() + "\n"


def _patch_function_docstring_in_source(source: str, docstring: str) -> str:
    module = ast.parse(source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(function_defs) != 1:
        return source
    function_def = function_defs[0]
    if not function_def.body:
        function_def.body.insert(0, ast.Expr(value=ast.Constant(value=docstring)))
        return ast.unparse(module) + "\n"
    first = function_def.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        first.value.value = docstring
        return ast.unparse(module) + "\n"
    function_def.body.insert(0, ast.Expr(value=ast.Constant(value=docstring)))
    return ast.unparse(module) + "\n"


def _normalize_singleton_response_docstring(
    response: SingletonRefactorResponse,
) -> SingletonRefactorResponse:
    symbol_def = _single_function_def(response.symbol_source)
    if symbol_def is None:
        return response
    source_docstring = _function_docstring(symbol_def)
    if source_docstring is None:
        return response
    normalized = _normalize_google_docstring(source_docstring)
    if normalized == source_docstring:
        return response
    patched_source = _patch_function_docstring_in_source(
        response.symbol_source,
        normalized,
    )
    return response.model_copy(
        update={
            "symbol_source": patched_source,
            "symbol_docstring": normalized,
        }
    )


def _normalize_cluster_response_docstring(
    response: ClusterRefactorResponse,
) -> ClusterRefactorResponse:
    helper_def = _single_function_def(response.helper_source)
    if helper_def is None:
        return response
    source_docstring = _function_docstring(helper_def)
    if source_docstring is None:
        return response
    normalized = _normalize_google_docstring(source_docstring)
    if normalized == source_docstring:
        return response
    patched_source = _patch_function_docstring_in_source(
        response.helper_source,
        normalized,
    )
    return response.model_copy(
        update={
            "helper_source": patched_source,
            "helper_docstring": normalized,
        }
    )


def _function_docstring(function_def: ast.FunctionDef) -> str | None:
    if not function_def.body:
        return None
    first = function_def.body[0]
    if not isinstance(first, ast.Expr):
        return None
    value = first.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _single_function_def(source: str) -> ast.FunctionDef | None:
    module = ast.parse(source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(function_defs) != 1:
        return None
    return function_defs[0]


def _docstring_from_function_source(source: str) -> str | None:
    """Return the AST docstring of the sole function in ``source``, if any."""
    function_def = _single_function_def(source)
    if function_def is None:
        return None
    return _function_docstring(function_def)


def _align_cluster_response_docstring(
    response: ClusterRefactorResponse,
) -> ClusterRefactorResponse:
    """Set ``helper_docstring`` from ``helper_source`` (source is authoritative)."""
    source_docstring = _docstring_from_function_source(response.helper_source)
    if source_docstring is None or source_docstring == response.helper_docstring:
        return response
    return response.model_copy(update={"helper_docstring": source_docstring})


def _align_singleton_response_docstring(
    response: SingletonRefactorResponse,
) -> SingletonRefactorResponse:
    """Set ``symbol_docstring`` from ``symbol_source`` (source is authoritative)."""
    source_docstring = _docstring_from_function_source(response.symbol_source)
    if source_docstring is None or source_docstring == response.symbol_docstring:
        return response
    return response.model_copy(update={"symbol_docstring": source_docstring})


def _normalize_member_key_concepts(
    response: ClusterRefactorResponse,
    *,
    key_vocabulary: tuple[KeyConceptSpec, ...],
) -> ClusterRefactorResponse:
    normalized_parameters: list[HelperParameter] = []
    for parameter in response.parameters:
        dimension_id = resolve_dimension_key(parameter.dimension_id, key_vocabulary)
        vocab_entry = next(
            item for item in key_vocabulary if item.dimension_id == dimension_id
        )
        if parameter.concept is not None and parameter.concept != vocab_entry.concept:
            raise ValueError(
                f"parameter {parameter.name!r} concept {parameter.concept!r} "
                f"does not match vocabulary concept {vocab_entry.concept!r} "
                f"for dimension_id {dimension_id!r}"
            )
        normalized_parameters.append(
            parameter.model_copy(
                update={
                    "dimension_id": dimension_id,
                    "concept": vocab_entry.concept,
                }
            )
        )
    dimension_id_by_name = {
        parameter.name: parameter.dimension_id for parameter in normalized_parameters
    }
    normalized_entries: list[MemberKeys] = []
    for entry in response.member_keys:
        normalized_entries_list: list[MemberKeyEntry] = []
        for key_entry in entry.keys:
            raw_key = dimension_id_by_name.get(
                key_entry.dimension_id, key_entry.dimension_id
            )
            dimension_id = resolve_dimension_key(raw_key, key_vocabulary)
            normalized_entries_list.append(
                MemberKeyEntry(dimension_id=dimension_id, value=key_entry.value)
            )
        normalized_entries.append(
            entry.model_copy(update={"keys": tuple(normalized_entries_list)})
        )
    return response.model_copy(
        update={
            "parameters": tuple(normalized_parameters),
            "member_keys": tuple(normalized_entries),
        }
    )


def _prepare_cluster_refactor_response(
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext,
) -> ClusterRefactorResponse:
    response = _normalize_member_key_concepts(
        response, key_vocabulary=ctx.key_vocabulary
    )
    response = _align_cluster_response_docstring(response)
    return _normalize_cluster_response_docstring(response)


def _prepare_singleton_refactor_response(
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
) -> SingletonRefactorResponse:
    _ = ctx
    response = _align_singleton_response_docstring(response)
    return _normalize_singleton_response_docstring(response)


EXCEL_SHAPED_LOCAL_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^_t\d+$"),
    re.compile(r"^t\d+$"),
    re.compile(r"^b\d+$"),
    re.compile(r"^col\d+$"),
    re.compile(r"^choose\d+$"),
    re.compile(r"^chooser\d+$"),
    re.compile(r"^func_map$"),
    re.compile(r"^input_?\d+$"),
    re.compile(r"^term\d+"),
)


def _names_from_target(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_names_from_target(element))
        return names
    return set()


def _argument_names(args: ast.arguments) -> set[str]:
    names = {arg.arg for arg in args.posonlyargs}
    names.update(arg.arg for arg in args.args)
    names.update(arg.arg for arg in args.kwonlyargs)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _local_binding_names(function_def: ast.FunctionDef) -> set[str]:
    parameter_names = _argument_names(function_def.args)
    names: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_names_from_target(target))
        elif isinstance(
            node,
            (ast.AnnAssign, ast.NamedExpr, ast.AugAssign, ast.For, ast.comprehension),
        ):
            names.update(_names_from_target(node.target))
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.Lambda):
            names.update(_argument_names(node.args))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node is function_def:
                continue
            names.add(node.name)
            names.update(_argument_names(node.args))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names.update(_names_from_target(node.optional_vars))
    return names - parameter_names


def is_excel_shaped_local_name(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in EXCEL_SHAPED_LOCAL_NAME_PATTERNS)


def validate_semantic_local_names(function_def: ast.FunctionDef) -> None:
    excel_shaped = sorted(
        name
        for name in _local_binding_names(function_def)
        if is_excel_shaped_local_name(name)
    )
    if excel_shaped:
        raise ValueError(
            "function body uses excel-shaped local names "
            f"{excel_shaped}; use domain-meaningful snake_case instead"
        )


def validate_no_cell_function_references(function_def: ast.FunctionDef) -> None:
    cell_references = sorted(
        {
            node.id
            for node in ast.walk(function_def)
            if isinstance(node, ast.Name) and node.id.startswith("cell_")
        }
    )
    if cell_references:
        raise ValueError(
            "function body must not reference excel cell helpers "
            f"{cell_references}; use semantic helpers from upstream refactors"
        )


def _is_xl_range_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "xl_range"
    )


def _names_bound_to_xl_range(function_def: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Assign) and _is_xl_range_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and _is_xl_range_call(node.value)
        ):
            names.add(node.target.id)
    return names


def _xl_index_ref_ref_arg(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "ref":
            return keyword.value
    return None


XL_INDEX_REF_OF_XL_RANGE_HINT = (
    "do not pass xl_range(...) into xl_index_ref; xl_index_ref expects a "
    "geometry tuple (sheet, row, col[, end_row, end_col]) or ExcelRange, not a "
    "Range from xl_range. Parameterize numeric coordinates from the exemplar's "
    "xl_index_ref((...), ...) call pattern"
)


def validate_no_xl_index_ref_of_xl_range(function_def: ast.FunctionDef) -> None:
    """Reject ``xl_index_ref(xl_range(...))`` and the bound-local equivalent.

    ``xl_range`` returns a lazy ``Range``; ``xl_index_ref`` only accepts
    geometry tuples or ``ExcelRange``. Passing a ``Range`` yields ``#VALUE!``
    at parity time.
    """
    xl_range_names = _names_bound_to_xl_range(function_def)
    for node in ast.walk(function_def):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "xl_index_ref":
            continue
        ref_arg = _xl_index_ref_ref_arg(node)
        if ref_arg is None:
            continue
        if _is_xl_range_call(ref_arg) or (
            isinstance(ref_arg, ast.Name) and ref_arg.id in xl_range_names
        ):
            raise ValueError(XL_INDEX_REF_OF_XL_RANGE_HINT)


def _suggested_param_name_by_dimension_id(
    key_vocabulary: tuple[KeyConceptSpec, ...],
) -> dict[str, str]:
    return {item.dimension_id: item.suggested_param_name for item in key_vocabulary}


def validate_parameter_names_match_vocabulary(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
) -> None:
    suggested = _suggested_param_name_by_dimension_id(ctx.key_vocabulary)
    mismatches = sorted(
        {
            f"{parameter.dimension_id!r}: expected "
            f"{suggested[parameter.dimension_id]!r}, "
            f"got {parameter.name!r}"
            for parameter in response.parameters
            if parameter.dimension_id in suggested
            and parameter.name != suggested[parameter.dimension_id]
        }
    )
    if mismatches:
        raise ValueError(
            "parameter names must match suggested_param_name from key_vocabulary: "
            + "; ".join(mismatches)
        )


def validate_allowed_global_references(
    function_def: ast.FunctionDef,
    *,
    allowed_names: set[str],
) -> None:
    parameter_names = _argument_names(function_def.args)
    local_names = _local_binding_names(function_def) | parameter_names
    builtin_names = set(dir(builtins))
    disallowed: set[str] = set()
    for node in ast.walk(function_def):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if (
                node.id in local_names
                or node.id in builtin_names
                or node.id in allowed_names
            ):
                continue
            disallowed.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            root = node.value.id
            if root in local_names or root in allowed_names or root in builtin_names:
                continue
            disallowed.add(root)
    if disallowed:
        raise ValueError(
            "helper_source references disallowed global names "
            f"{sorted(disallowed)}; allowed: {sorted(allowed_names)}"
        )


def _without_cell_function_names(names: set[str]) -> set[str]:
    """Drop ``cell_*`` names from refactor allowlists.

    Helper bodies must not reference excel cell wrappers
    (``validate_no_cell_function_references``), so including those names in the
    allowlist only inflates validation errors for large range dependencies.
    """
    return {name for name in names if not name.startswith("cell_")}


def validate_cluster_refactor_response(
    ctx: ClusterRefactorContext,
    response: ClusterRefactorResponse,
    *,
    existing_names: frozenset[str],
    internals_source: str,
    require_semantic_locals: bool = True,
) -> None:
    if response.helper_name != ctx.expected_helper_name:
        raise ValueError(
            "helper_name must equal locked helper name "
            f"{ctx.expected_helper_name!r}, got {response.helper_name!r}"
        )
    validate_semantic_identifier(
        response.helper_name,
        existing_names=existing_names,
        allow_name=ctx.expected_helper_name,
    )

    member_key_addresses = {entry.address for entry in response.member_keys}
    member_addresses = {member.address for member in ctx.members}
    if member_key_addresses != member_addresses:
        raise ValueError(
            "member_keys must cover cluster members exactly: "
            f"expected {sorted(member_addresses)}, got {sorted(member_key_addresses)}"
        )

    parameter_dimension_ids = {
        parameter.dimension_id for parameter in response.parameters
    }
    vocabulary_dimension_ids = {item.dimension_id for item in ctx.key_vocabulary}
    unknown_parameters = sorted(parameter_dimension_ids - vocabulary_dimension_ids)
    if unknown_parameters:
        raise ValueError(
            f"parameters reference unknown binding dimensions: {unknown_parameters}"
        )

    parameter_names = [parameter.name for parameter in response.parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise ValueError("parameter names must be unique")
    for parameter in response.parameters:
        if not parameter.name.isidentifier():
            raise ValueError(
                f"parameter name is not a valid identifier: {parameter.name!r}"
            )

    dimension_sets = [
        frozenset(ctx.expected_member_keys[member.address].keys())
        for member in ctx.members
    ]
    if len(set(dimension_sets)) != 1:
        raise ValueError("cluster members must share one varying key dimension set")
    varying_dimension_ids = dimension_sets[0]
    if ctx.contract == "dimension_aware":
        collapsed = [
            f"concept {concept!r} requires one parameter per dimension id "
            f"{sorted(dimension_ids)}"
            for concept, dimension_ids in sorted(
                concepts_with_multiple_dimensions(
                    varying_dimension_ids, ctx.key_vocabulary
                ).items()
            )
            if not set(dimension_ids) <= parameter_dimension_ids
        ]
        if collapsed:
            raise ValueError(
                "dimension-aware response collapses distinct dimensions onto one "
                "concept parameter: " + "; ".join(collapsed)
            )
    if parameter_dimension_ids != varying_dimension_ids:
        raise ValueError(
            "parameters must match varying binding key dimensions for the cluster: "
            f"expected {sorted(varying_dimension_ids)}, "
            f"got {sorted(parameter_dimension_ids)}"
        )

    seen_member_key_combinations: set[tuple[tuple[str, BindingKeyValue], ...]] = set()
    for entry in response.member_keys:
        member = next(item for item in ctx.members if item.address == entry.address)
        if entry.function_name != member.function_name:
            raise ValueError(
                f"member_keys for {entry.address} has function_name "
                f"{entry.function_name!r}, expected {member.function_name!r}"
            )
        entry_keys = entry.keys_dict()
        extra_dimensions = set(entry_keys) - parameter_dimension_ids
        if extra_dimensions:
            raise ValueError(
                f"member_keys for {entry.address} must not include "
                f"series-constant dimensions: {sorted(extra_dimensions)}"
            )
        missing_dimensions = parameter_dimension_ids - set(entry_keys)
        if missing_dimensions:
            raise ValueError(
                f"member_keys for {entry.address} missing parameter dimensions: "
                f"{sorted(missing_dimensions)}"
            )
        key_combination = tuple(sorted(entry_keys.items()))
        # Possibly this expectation should be changed.
        # There may be times when two cell formulas are functionally identical
        # or can be represented with identical semantics, so unique cell-wise
        # triangulation is not required to route to the correct semantics.
        if key_combination in seen_member_key_combinations:
            raise ValueError(
                "member_keys must have a unique key combination for each member"
            )
        seen_member_key_combinations.add(key_combination)
        expected_keys = ctx.expected_member_keys[entry.address]
        for dimension_id, expected_value in expected_keys.items():
            actual_value = entry_keys.get(dimension_id)
            if actual_value != expected_value:
                raise ValueError(
                    f"member_keys for {entry.address} has "
                    f"{dimension_id}={actual_value!r}, "
                    f"expected {expected_value!r}"
                )

    if not response.helper_name.isidentifier():
        raise ValueError(
            f"helper_name is not a valid identifier: {response.helper_name!r}"
        )

    helper_module = ast.parse(response.helper_source)
    function_defs = [
        node for node in helper_module.body if isinstance(node, ast.FunctionDef)
    ]
    if len(function_defs) != 1:
        raise ValueError("helper_source must contain exactly one FunctionDef")
    helper_def = function_defs[0]
    if helper_def.name != response.helper_name:
        raise ValueError(
            "helper_source function name must match helper_name "
            f"({response.helper_name!r})"
        )

    source_docstring = _function_docstring(helper_def)
    if source_docstring is None:
        raise ValueError("helper_source must include a docstring")
    validate_google_style_docstring(source_docstring)

    validate_helper_serves_member_keys(
        helper_def,
        helper_name=response.helper_name,
        parameter_names_by_dimension={
            parameter.dimension_id: parameter.name for parameter in response.parameters
        },
        member_keys_by_address={
            entry.address: entry.keys_dict() for entry in response.member_keys
        },
    )

    if require_semantic_locals:
        validate_semantic_local_names(helper_def)
    validate_no_cell_function_references(helper_def)
    validate_no_xl_index_ref_of_xl_range(helper_def)
    validate_parameter_names_match_vocabulary(ctx, response)

    arg_names = [arg.arg for arg in helper_def.args.args]
    expected_args = ["ctx", *[parameter.name for parameter in response.parameters]]
    if arg_names != expected_args:
        raise ValueError(
            f"helper must accept exactly ({', '.join(expected_args)}); "
            f"got ({', '.join(arg_names)})"
        )

    if any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for node in ast.walk(helper_module)
    ):
        raise ValueError("helper_source must not contain imports")

    allowed_names = _without_cell_function_names(
        set(ctx.allowed_runtime_symbols)
        | set(ctx.external_dependencies)
        | {member.function_name for member in ctx.members}
        | {response.helper_name}
        | semantic_helpers_available_for_calls(internals_source, existing_names)
        | set(ALLOWED_REFACTOR_TYPE_HINT_NAMES)
        | {"ctx"}
        | {parameter.name for parameter in response.parameters}
        # Always allow helper-memoization symbols; Pass 1 patches runtime.py
        # when they are missing from older embedded exports.
        | {"xl_helper", "xl_memoize"}
    )
    builtin_names = set(dir(builtins))
    for node in ast.walk(helper_def):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in builtin_names or name in allowed_names:
            continue
        raise ValueError(
            f"helper_source calls disallowed function {name!r}; "
            f"allowed: {sorted(allowed_names)}"
        )

    validate_allowed_global_references(helper_def, allowed_names=allowed_names)


def singleton_refactor_cache_key(
    ctx: SingletonRefactorContext,
    internals_bytes: bytes,
    response_schema: dict[str, object],
) -> str:
    payload = {
        "kind": "singleton",
        "model": refactor_model(),
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "address": ctx.address,
        "canonical_template": ctx.canonical_template,
        "formula_sha256": hashlib.sha256(ctx.normalized_formula.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(ctx.python_source.encode()).hexdigest(),
        "internals_sha256": hashlib.sha256(
            _normalize_internals_bytes(internals_bytes)
        ).hexdigest(),
        "response_schema_sha256": hashlib.sha256(
            stable_json(response_schema).encode()
        ).hexdigest(),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def semantic_naming_cache_payload(
    *,
    kind: Literal["cluster", "singleton"],
    unit_id: str,
    canonical_template: str,
    mechanical_body: str,
    response_schema: dict[str, object],
    member_fingerprints: Sequence[Sequence[object]],
    contract: str | None = None,
) -> dict[str, object]:
    """Build the cache payload for a pass-2 semantic naming request.

    The payload is keyed to what actually determines the naming answer: the
    model, prompt version, the mechanical body being named, the response schema,
    and per-member fingerprints. It deliberately omits ``internals_sha256`` so a
    naming result survives unrelated edits elsewhere in ``internals.py`` — the
    mechanical body already encodes everything the semantic layer depends on.
    """
    payload: dict[str, object] = {
        "model": refactor_model(),
        "prompt_version": REFACTOR_PROMPT_VERSION,
        "kind": kind,
        "unit_id": unit_id,
        "canonical_template": canonical_template,
        "mechanical_body_sha256": hashlib.sha256(mechanical_body.encode()).hexdigest(),
        "response_schema_sha256": hashlib.sha256(
            stable_json(response_schema).encode()
        ).hexdigest(),
        "member_fingerprints": [list(entry) for entry in member_fingerprints],
    }
    if kind == "cluster":
        payload["contract"] = contract
    return payload


def semantic_naming_cache_key(
    *,
    kind: Literal["cluster", "singleton"],
    unit_id: str,
    canonical_template: str,
    mechanical_body: str,
    response_schema: dict[str, object],
    member_fingerprints: Sequence[Sequence[object]],
    contract: str | None = None,
) -> str:
    """Return the sha256 hex of the stable-JSON semantic naming payload."""
    payload = semantic_naming_cache_payload(
        kind=kind,
        unit_id=unit_id,
        canonical_template=canonical_template,
        mechanical_body=mechanical_body,
        response_schema=response_schema,
        member_fingerprints=member_fingerprints,
        contract=contract,
    )
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def mechanical_placeholder_docstring(parameter_names: Sequence[str]) -> str:
    """Return a valid Google-style placeholder docstring for a mechanical body.

    Pass 1 must materialize a validated helper before the LLM has named it, so
    the body carries this deterministic placeholder documentation until pass 2
    replaces it with the model's docstring.
    """
    lines = [
        "Mechanically synthesized helper pending semantic naming.",
        "",
        "Args:",
        "    ctx: Workbook evaluation context.",
    ]
    for name in parameter_names:
        lines.append(f"    {name}: Projection key parameter.")
    lines.extend(["", "Returns:", "    Cell value."])
    return "\n".join(lines)


def load_singleton_refactor_prompt_fixed_portion() -> str:
    return SINGLETON_REFACTOR_PROMPT_FIXTURE.read_text(encoding="utf-8")


def strip_python_string_delimiters(docstring: str) -> str:
    stripped = docstring.strip()
    for quote in ('"""', "'''"):
        if stripped.startswith(quote) and stripped.endswith(quote):
            inner = stripped[len(quote) : -len(quote)]
            inner = inner.removeprefix("\n")
            return inner.rstrip("\n")
    return docstring


def append_refactor_note_section(
    docstring: str,
    *,
    address: str,
    formula: str,
) -> str:
    return f"{docstring.rstrip()}\n\nNote:\n    Covers {address}. Excel: {formula}."


def _format_docstring_expression(docstring: str) -> str:
    """Return Python source for an expression whose value is ``docstring``.

    Prefer a readable triple-quoted literal when embedding cannot reinterpret or
    truncate the text; otherwise emit an ``ast.unparse``-escaped constant so
    backslashes and quotes in Excel notes round-trip through ``ast.parse``.
    """
    if "\\" not in docstring and '"""' not in docstring:
        return f'"""{docstring}"""'
    return ast.unparse(ast.Constant(value=docstring))


def assemble_singleton_symbol_source(
    *,
    signature: str,
    docstring: str,
    body: str,
) -> str:
    normalized = textwrap.dedent(docstring).strip()
    if not normalized:
        raise ValueError("docstring must not be empty")
    body_block = "\n".join(f"    {line}" for line in body.splitlines()) + "\n"
    # Close a triple-quoted literal on the same line as the last docstring
    # content when that form is safe. A newline + indented closing """ would
    # become part of the AST string value.
    docstring_expr = _format_docstring_expression(normalized)
    return f"{signature}\n    {docstring_expr}\n{body_block}"


def inject_signature_return_type_hint(signature: str, return_hint: str) -> str:
    """Replace or append an allowlisted return hint on a function signature line."""
    stripped = signature.strip()
    without_return = re.sub(r"\s*->\s*.+$", "", stripped).rstrip(":").rstrip()
    return f"{without_return} -> {return_hint}:"


def build_locked_helper_signature(
    helper_name: str,
    *,
    parameters: Sequence[HelperParameter] = (),
) -> str:
    """Build ``def {helper_name}(ctx: EvalContext, ...)`` from locked name + params."""
    parts = ["ctx: EvalContext"]
    for parameter in parameters:
        python_type = _binding_dtype_to_python(parameter.dtype) or parameter.dtype
        parts.append(f"{parameter.name}: {python_type}")
    return f"def {helper_name}({', '.join(parts)}):"


def prepare_singleton_refactor_response(
    llm_response: SingletonRefactorLLMResponse,
    ctx: SingletonRefactorContext,
    *,
    runtime_source: str,
    internals_source: str,
    callee_hints: Mapping[str, str] | None = None,
) -> SingletonRefactorResponse:
    if llm_response.symbol_docstring is None or llm_response.symbol_body is None:
        raise ValueError(
            "singleton refactor response is missing required success fields"
        )
    return_hint = infer_refactor_return_type_hint(
        python_sources=(ctx.python_source,),
        runtime_source=runtime_source,
        internals_source=internals_source,
        naming_hints=ctx.naming_hints,
        callee_hints=callee_hints,
    )
    signature = inject_signature_return_type_hint(
        build_locked_helper_signature(ctx.expected_helper_name),
        return_hint,
    )
    docstring = strip_python_string_delimiters(llm_response.symbol_docstring)
    docstring = append_refactor_note_section(
        docstring,
        address=ctx.address,
        formula=ctx.normalized_formula,
    )
    symbol_source = assemble_singleton_symbol_source(
        signature=signature,
        docstring=docstring,
        body=llm_response.symbol_body,
    )
    source_docstring = _docstring_from_function_source(symbol_source)
    if source_docstring is None:
        raise ValueError("assembled symbol_source must include a docstring")
    return SingletonRefactorResponse(
        symbol_name=ctx.expected_helper_name,
        symbol_docstring=source_docstring,
        symbol_source=symbol_source,
    )


def _format_binding_map_yaml(
    label: str,
    values: object,
) -> list[str]:
    if not isinstance(values, Mapping) or not values:
        return [f"{label}: {{}}"]
    lines = [f"{label}:"]
    for concept, value in sorted(values.items(), key=lambda item: item[0]):
        lines.append(f"  {concept}: {_yaml_scalar(value)}")
    return lines


def _format_cell_metadata_yaml(cell_metadata: Mapping[str, object]) -> str:
    lines = [f"address: {cell_metadata['address']}"]
    lines.extend(
        _format_binding_map_yaml("binding_keys", cell_metadata.get("binding_keys"))
    )
    lines.extend(
        _format_binding_map_yaml("binding_record", cell_metadata.get("binding_record"))
    )
    return "\n".join(lines)


def format_singleton_refactor_context_dump(
    *,
    function_source: str,
    cell_metadata: Mapping[str, object],
    dependency_stubs: str,
    helper_name: str | None = None,
) -> str:
    yaml_block = _format_cell_metadata_yaml(cell_metadata)
    header = "Function to refactor:\n\n"
    if helper_name is not None:
        header = f"Function to refactor (helper_name={helper_name}):\n\n"
    return (
        f"{header}"
        f"```python\n{function_source.strip()}\n```\n\n"
        "Cell metadata:\n\n"
        f"```yaml\n{yaml_block}\n```\n\n"
        "Dependencies:\n\n"
        f"```python\n{dependency_stubs.strip()}\n```"
    )


def _function_defs_by_name(
    source: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> dict[str, ast.FunctionDef]:
    if index is not None:
        return dict(index.functions)
    module = ast.parse(source)
    return {
        node.name: node for node in module.body if isinstance(node, ast.FunctionDef)
    }


def _called_function_names(function_source: str) -> tuple[str, ...]:
    module = ast.parse(function_source)
    function_defs = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(function_defs) != 1:
        raise ValueError("function_source must contain exactly one FunctionDef")
    function_def = function_defs[0]
    names: list[str] = []
    for node in ast.walk(function_def):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.append(node.func.id)
    return tuple(dict.fromkeys(names))


def _strip_note_section(docstring: str) -> str:
    dedented = textwrap.dedent(docstring).strip()
    for pattern in (r"\n\nNote:\n.*\Z", r"\nNote:\n.*\Z"):
        match = re.search(pattern, dedented, re.DOTALL)
        if match is not None:
            return dedented[: match.start()].rstrip()
    return dedented


def _llm_dependency_return_suffix(function_def: ast.FunctionDef) -> str:
    known = KNOWN_RUNTIME_RETURN_HINTS.get(function_def.name)
    if known is not None:
        return f" -> {known}"
    if function_def.returns is None:
        return ""
    hint = ast.unparse(function_def.returns).strip()
    normalized = normalize_return_type_hint_for_allowlist(hint)
    if normalized is not None:
        return f" -> {normalized}"
    return ""


def _llm_dependency_signature_line(function_def: ast.FunctionDef) -> str:
    args = ast.unparse(function_def.args)
    return_suffix = _llm_dependency_return_suffix(function_def)
    return f"def {function_def.name}({args}){return_suffix}:"


def _format_runtime_dependency_stub(function_def: ast.FunctionDef) -> str:
    docstring = _function_docstring(function_def)
    lines = [_llm_dependency_signature_line(function_def)]
    if docstring is not None:
        if "\n" in docstring:
            compact = " ".join(docstring.split())
            lines.append(f'    """{compact}"""')
        else:
            lines.append(f'    """{docstring}"""')
    lines.append("    # ...")
    return "\n".join(lines)


def _format_semantic_dependency_stub(function_def: ast.FunctionDef) -> str:
    docstring = _function_docstring(function_def)
    lines = [f"def {function_def.name}(ctx: EvalContext) -> float:"]
    if docstring is not None:
        trimmed = _strip_note_section(docstring)
        lines.append('    """')
        for line in trimmed.splitlines():
            lines.append(f"    {line}" if line else "")
        lines.append('    """')
    lines.append("    # ...")
    return "\n".join(lines)


def _build_dependency_stubs(
    *,
    function_source: str,
    internals_source: str,
    runtime_source: str,
    index: InternalsSourceIndex | None = None,
) -> str:
    called = _called_function_names(function_source)
    runtime_defs = _function_defs_by_name(runtime_source)
    internals_defs = _function_defs_by_name(internals_source, index=index)
    runtime_names = sorted(name for name in called if name in runtime_defs)
    semantic_names = sorted(
        name
        for name in called
        if name in internals_defs and not name.startswith("cell_")
    )
    stubs = [
        *(
            _format_runtime_dependency_stub(runtime_defs[name])
            for name in runtime_names
        ),
        *(
            _format_semantic_dependency_stub(internals_defs[name])
            for name in semantic_names
        ),
    ]
    return "\n\n".join(stubs)


def build_singleton_refactor_context_dump(
    *,
    function_name: str,
    address: str,
    internals_source: str,
    runtime_source: str,
    cell_metadata: Mapping[str, object],
    index: InternalsSourceIndex | None = None,
    function_source: str | None = None,
    helper_name: str | None = None,
) -> str:
    resolved = (
        index
        if index is not None
        else InternalsSourceIndex.from_source(internals_source)
    )
    resolved_function_source = (
        function_source
        if function_source is not None
        else resolved.function_source(function_name)
    )
    metadata = dict(cell_metadata)
    metadata["address"] = address
    dependency_stubs = _build_dependency_stubs(
        function_source=resolved_function_source,
        internals_source=internals_source,
        runtime_source=runtime_source,
        index=resolved,
    )
    return format_singleton_refactor_context_dump(
        function_source=resolved_function_source,
        cell_metadata=metadata,
        dependency_stubs=dependency_stubs,
        helper_name=helper_name,
    )


def _cell_metadata_for_singleton_refactor(
    ctx: SingletonRefactorContext,
) -> dict[str, object]:
    metadata: dict[str, object] = {"address": ctx.address}
    binding_keys = ctx.naming_hints.get("binding_keys")
    binding_record = ctx.naming_hints.get("binding_record")
    metadata["binding_keys"] = (
        dict(binding_keys) if isinstance(binding_keys, Mapping) else {}
    )
    metadata["binding_record"] = (
        dict(binding_record) if isinstance(binding_record, Mapping) else {}
    )
    return metadata


def build_singleton_refactor_prompt_context(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    runtime_path: Path | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> str:
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    return build_singleton_refactor_context_dump(
        function_name=ctx.function_name,
        address=ctx.address,
        internals_source=index.source,
        runtime_source=_read_runtime_source(internals_path, runtime_path=runtime_path),
        cell_metadata=_cell_metadata_for_singleton_refactor(ctx),
        index=index,
        function_source=ctx.python_source,
        helper_name=ctx.expected_helper_name,
    )


def load_cluster_refactor_prompt_fixed_portion(
    contract: ClusterRefactorContract = "member_sweep",
) -> str:
    return CLUSTER_REFACTOR_PROMPT_FIXTURES[contract].read_text(encoding="utf-8")


def append_cluster_refactor_note_section(
    docstring: str,
    *,
    covered_addresses: str,
    formula: str,
) -> str:
    return (
        f"{docstring.rstrip()}\n\n"
        f"Note:\n    Covers {covered_addresses}. Excel: {formula}."
    )


def format_cluster_covered_addresses(addresses: Sequence[str]) -> str:
    if not addresses:
        raise ValueError("addresses must not be empty")
    if len(addresses) == 1:
        return addresses[0]
    parsed = [parse_workbook_address(address) for address in addresses]
    sheets = {sheet for sheet, _column, _row in parsed}
    rows = {row for _sheet, _column, row in parsed}
    if len(sheets) != 1 or len(rows) != 1:
        return ", ".join(sorted(addresses))
    sheet = next(iter(sheets))
    row = next(iter(rows))
    columns = sorted(
        {column for _sheet, column, _row in parsed},
        key=_column_index,
    )
    column_indices = [_column_index(column) for column in columns]
    contiguous = all(
        later - earlier == 1 for earlier, later in itertools.pairwise(column_indices)
    )
    if contiguous:
        return f"{sheet}!{columns[0]}{row}:{columns[-1]}{row}"
    return ", ".join(sorted(addresses))


def assemble_cluster_symbol_source(
    *,
    signature: str,
    docstring: str,
    body: str,
) -> str:
    return assemble_singleton_symbol_source(
        signature=signature,
        docstring=docstring,
        body=body,
    )


def synthesize_cluster_parameters(
    ctx: ClusterRefactorContext,
) -> tuple[HelperParameter, ...]:
    """Build helper parameters from varying dimensions × key vocabulary."""
    dimension_sets = [
        frozenset(ctx.expected_member_keys[member.address].keys())
        for member in ctx.members
    ]
    if not dimension_sets:
        return ()
    if len(set(dimension_sets)) != 1:
        raise ValueError("cluster members must share one varying key dimension set")
    varying_dimension_ids = dimension_sets[0]
    specs = helper_parameters_for_varying_keys(
        varying_dimension_ids, ctx.key_vocabulary
    )
    return tuple(
        HelperParameter(
            name=spec.suggested_param_name,
            dimension_id=spec.dimension_id,
            dtype=spec.dtype,
            concept=spec.concept,
        )
        for spec in specs
    )


def synthesize_cluster_member_keys(
    ctx: ClusterRefactorContext,
    *,
    parameters: Sequence[HelperParameter],
) -> tuple[MemberKeys, ...]:
    """Build full-cluster member_keys from expected binding keys."""
    return complete_cluster_member_keys_from_expected(
        (),
        ctx,
        parameters=parameters,
    )


def prepare_cluster_refactor_response(
    llm_response: ClusterRefactorLLMResponse,
    ctx: ClusterRefactorContext,
    *,
    runtime_source: str,
    internals_source: str,
    callee_hints: Mapping[str, str] | None = None,
) -> ClusterRefactorResponse:
    if llm_response.symbol_docstring is None or llm_response.symbol_body is None:
        raise ValueError("cluster refactor response is missing required success fields")
    parameters = synthesize_cluster_parameters(ctx)
    member_keys = synthesize_cluster_member_keys(ctx, parameters=parameters)
    return_hint = infer_refactor_return_type_hint(
        python_sources=tuple(member.python_source for member in ctx.members),
        runtime_source=runtime_source,
        internals_source=internals_source,
        naming_hints=ctx.naming_hints,
        callee_hints=callee_hints,
    )
    signature = inject_signature_return_type_hint(
        build_locked_helper_signature(
            ctx.expected_helper_name,
            parameters=parameters,
        ),
        return_hint,
    )
    docstring = strip_python_string_delimiters(llm_response.symbol_docstring)
    covered_addresses = format_cluster_covered_addresses(
        tuple(member.address for member in ctx.members)
    )
    docstring = append_cluster_refactor_note_section(
        docstring,
        covered_addresses=covered_addresses,
        formula=ctx.canonical_template,
    )
    helper_source = assemble_cluster_symbol_source(
        signature=signature,
        docstring=docstring,
        body=llm_response.symbol_body,
    )
    source_docstring = _docstring_from_function_source(helper_source)
    if source_docstring is None:
        raise ValueError("assembled helper_source must include a docstring")
    return ClusterRefactorResponse(
        helper_name=ctx.expected_helper_name,
        helper_docstring=source_docstring,
        helper_source=helper_source,
        parameters=parameters,
        member_keys=member_keys,
    )


def build_mechanical_cluster_response(
    ctx: ClusterRefactorContext,
    draft: MechanicalBodyDraft,
    *,
    runtime_source: str,
    internals_source: str,
    callee_hints: Mapping[str, str] | None = None,
) -> ClusterRefactorResponse:
    """Assemble a pass-1 cluster response from a verified mechanical draft body.

    The draft body still uses mechanical local names (``_t1`` ...); the semantic
    layer (docstring and local names) is deferred to pass 2. A deterministic
    placeholder docstring keeps the helper valid until then.

    Mechanical helpers are decorated with ``@xl_memoize`` so period-recurrence
    chains share work under a warm ``EvalContext`` (library-visible caching).
    """
    from src.helper_memoization import apply_xl_memoize_decorator

    parameters = synthesize_cluster_parameters(ctx)
    llm_response = ClusterRefactorLLMResponse(
        symbol_docstring=mechanical_placeholder_docstring(
            [parameter.name for parameter in parameters]
        ),
        symbol_body=draft.body,
        error=None,
        error_reason=None,
    )
    response = prepare_cluster_refactor_response(
        llm_response,
        ctx,
        runtime_source=runtime_source,
        internals_source=internals_source,
        callee_hints=callee_hints,
    )
    return response.model_copy(
        update={"helper_source": apply_xl_memoize_decorator(response.helper_source)}
    )


def build_mechanical_singleton_response(
    ctx: SingletonRefactorContext,
    draft: MechanicalBodyDraft,
    *,
    runtime_source: str,
    internals_source: str,
    callee_hints: Mapping[str, str] | None = None,
) -> SingletonRefactorResponse:
    """Assemble a pass-1 singleton response from a mechanical draft body.

    As with clusters, the mechanical local names survive into pass 1 behind a
    placeholder docstring; pass 2 renames them and supplies the real docstring.
    """
    from src.helper_memoization import apply_xl_memoize_decorator

    llm_response = SingletonRefactorLLMResponse(
        symbol_docstring=mechanical_placeholder_docstring(()),
        symbol_body=draft.body,
        error=None,
        error_reason=None,
    )
    response = prepare_singleton_refactor_response(
        llm_response,
        ctx,
        runtime_source=runtime_source,
        internals_source=internals_source,
        callee_hints=callee_hints,
    )
    return response.model_copy(
        update={"symbol_source": apply_xl_memoize_decorator(response.symbol_source)}
    )


def _yaml_scalar(value: object) -> str:
    if isinstance(value, str):
        if (
            not value
            or value.isdigit()
            or value != value.strip()
            or value.startswith(("'", '"'))
            or ":" in value
            or "#" in value
        ):
            return json.dumps(value)
        return value
    return str(value)


def _format_key_vocabulary_yaml(
    key_vocabulary: Sequence[KeyConceptSpec],
) -> str:
    lines: list[str] = []
    for item in key_vocabulary:
        lines.append(f"- dimension_id: {item.dimension_id}")
        lines.append(f"  concept: {item.concept}")
        lines.append(f"  dtype: {item.dtype}")
        lines.append(f"  suggested_param_name: {item.suggested_param_name}")
    return "\n".join(lines)


def _format_member_metadata_yaml(
    member_metadata: Sequence[Mapping[str, object]],
) -> str:
    blocks: list[str] = []
    for entry in member_metadata:
        lines = [
            f"- address: {entry['address']}",
            f"  function_name: {entry['function_name']}",
            "  expected_keys:",
        ]
        expected_keys = entry.get("expected_keys", {})
        if isinstance(expected_keys, Mapping):
            for concept, value in expected_keys.items():
                lines.append(f"    {concept}: {value}")
        for map_key in ("binding_keys", "binding_record"):
            map_lines = _format_binding_map_yaml(map_key, entry.get(map_key))
            lines.extend(f"  {line}" for line in map_lines)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def format_cluster_refactor_context_dump(
    *,
    member_sources: str,
    key_vocabulary: Sequence[KeyConceptSpec],
    member_metadata: Sequence[Mapping[str, object]],
    dependency_stubs: str,
    shown_member_count: int | None = None,
    total_member_count: int | None = None,
) -> str:
    vocabulary_yaml = _format_key_vocabulary_yaml(key_vocabulary)
    metadata_yaml = _format_member_metadata_yaml(member_metadata)
    if (
        shown_member_count is not None
        and total_member_count is not None
        and shown_member_count < total_member_count
    ):
        heading = (
            f"Cluster to refactor (showing {shown_member_count} of "
            f"{total_member_count} members; remaining member_keys are filled "
            "mechanically):\n\n"
        )
    else:
        heading = "Cluster to refactor:\n\n"
    return (
        f"{heading}"
        f"```python\n{member_sources.strip()}\n```\n\n"
        "Key vocabulary:\n\n"
        f"```yaml\n{vocabulary_yaml}\n```\n\n"
        "Member metadata:\n\n"
        f"```yaml\n{metadata_yaml}\n```\n\n"
        "Dependencies:\n\n"
        f"```python\n{dependency_stubs.strip()}\n```"
    )


def _called_function_names_from_sources(
    function_sources: Iterable[str],
) -> tuple[str, ...]:
    names: list[str] = []
    for function_source in function_sources:
        names.extend(_called_function_names(function_source))
    return tuple(dict.fromkeys(names))


def _build_cluster_dependency_stubs(
    *,
    member_function_names: Sequence[str],
    internals_source: str,
    runtime_source: str,
    index: InternalsSourceIndex | None = None,
    member_function_sources: Mapping[str, str] | None = None,
) -> str:
    resolved = (
        index
        if index is not None
        else InternalsSourceIndex.from_source(internals_source)
    )
    if member_function_sources is None:
        function_sources = [
            resolved.function_source(function_name)
            for function_name in member_function_names
        ]
    else:
        function_sources = [
            member_function_sources[function_name]
            for function_name in member_function_names
        ]
    called = _called_function_names_from_sources(function_sources)
    runtime_defs = _function_defs_by_name(runtime_source)
    internals_defs = resolved.functions
    runtime_names = sorted(name for name in called if name in runtime_defs)
    semantic_names = sorted(
        name
        for name in called
        if name in internals_defs and not name.startswith("cell_")
    )
    stubs = [
        *(
            _format_runtime_dependency_stub(runtime_defs[name])
            for name in runtime_names
        ),
        *(
            _format_semantic_dependency_stub(internals_defs[name])
            for name in semantic_names
        ),
    ]
    return "\n\n".join(stubs)


def sample_indices_for_prompt(
    count: int,
    *,
    limit: int = CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT,
) -> tuple[int, ...]:
    """Return up to ``limit`` indices spaced across ``0..count-1`` inclusive."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if count <= limit:
        return tuple(range(count))
    if limit == 1:
        return (0,)
    return tuple(
        dict.fromkeys(round(i * (count - 1) / (limit - 1)) for i in range(limit))
    )


def sample_members_for_prompt[T](
    members: Sequence[T],
    *,
    limit: int = CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT,
) -> tuple[T, ...]:
    """Return a uniformly spaced subset of ``members`` capped at ``limit``."""
    indices = sample_indices_for_prompt(len(members), limit=limit)
    return tuple(members[index] for index in indices)


def complete_cluster_member_keys_from_expected(
    member_keys: Sequence[MemberKeys],
    ctx: ClusterRefactorContext,
    *,
    parameters: Sequence[HelperParameter],
) -> tuple[MemberKeys, ...]:
    """Fill missing member_keys from expected binding keys for the full cluster."""
    parameter_dimension_ids = tuple(
        dict.fromkeys(parameter.dimension_id for parameter in parameters)
    )
    by_address = {entry.address: entry for entry in member_keys}
    completed: list[MemberKeys] = []
    for member in ctx.members:
        existing = by_address.get(member.address)
        if existing is not None:
            completed.append(existing)
            continue
        expected = ctx.expected_member_keys.get(member.address, {})
        keys = tuple(
            MemberKeyEntry(dimension_id=dimension_id, value=expected[dimension_id])
            for dimension_id in parameter_dimension_ids
            if dimension_id in expected
        )
        completed.append(
            MemberKeys(
                address=member.address,
                function_name=member.function_name,
                keys=keys,
            )
        )
    return tuple(completed)


def build_cluster_refactor_context_dump(
    *,
    member_function_names: Sequence[str],
    internals_source: str,
    runtime_source: str,
    key_vocabulary: Sequence[KeyConceptSpec],
    member_metadata: Sequence[Mapping[str, object]],
    shown_member_count: int | None = None,
    total_member_count: int | None = None,
    index: InternalsSourceIndex | None = None,
    member_function_sources: Mapping[str, str] | None = None,
) -> str:
    resolved = (
        index
        if index is not None
        else InternalsSourceIndex.from_source(internals_source)
    )
    if member_function_sources is None:
        resolved_member_sources = {
            function_name: resolved.function_source(function_name)
            for function_name in member_function_names
        }
    else:
        resolved_member_sources = {
            function_name: member_function_sources.get(
                function_name,
                resolved.function_source(function_name),
            )
            for function_name in member_function_names
        }
    member_sources = "\n\n\n".join(
        resolved_member_sources[function_name].strip()
        for function_name in member_function_names
    )
    dependency_stubs = _build_cluster_dependency_stubs(
        member_function_names=member_function_names,
        internals_source=internals_source,
        runtime_source=runtime_source,
        index=resolved,
        member_function_sources=resolved_member_sources,
    )
    return format_cluster_refactor_context_dump(
        member_sources=member_sources,
        key_vocabulary=key_vocabulary,
        member_metadata=member_metadata,
        dependency_stubs=dependency_stubs,
        shown_member_count=shown_member_count,
        total_member_count=total_member_count,
    )


def _member_metadata_for_cluster_refactor(
    ctx: ClusterRefactorContext,
    *,
    members: Sequence[MemberContext] | None = None,
) -> tuple[dict[str, object], ...]:
    selected_members = ctx.members if members is None else tuple(members)
    entries: list[dict[str, object]] = []
    for member in selected_members:
        entry: dict[str, object] = {
            "address": member.address,
            "function_name": member.function_name,
            "expected_keys": ctx.expected_member_keys.get(member.address, {}),
            "binding_keys": member.binding_keys or {},
            "binding_record": member.binding_record or {},
        }
        entries.append(entry)
    return tuple(entries)


def build_cluster_refactor_prompt_context(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    runtime_path: Path | None = None,
    member_limit: int = CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT,
    internals_index: InternalsSourceIndex | None = None,
) -> str:
    varying_dimension_ids = frozenset(
        dimension_id
        for keys in ctx.expected_member_keys.values()
        for dimension_id in keys
    )
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    filtered_vocabulary = tuple(
        item
        for item in ctx.key_vocabulary
        if item.dimension_id in varying_dimension_ids
    )
    vocabulary_yaml = _format_key_vocabulary_yaml(filtered_vocabulary)
    runtime_source = _read_runtime_source(internals_path, runtime_path=runtime_path)

    summary = ctx.fingerprint_summary
    use_fingerprint = summary is not None and summary.fallback_reason is None
    if use_fingerprint:
        assert summary is not None
        exemplars = tuple(group.exemplar for group in summary.groups)
        member_function_sources = {
            member.function_name: member.python_source for member in exemplars
        }
        dependency_stubs = _build_cluster_dependency_stubs(
            member_function_names=tuple(member.function_name for member in exemplars),
            internals_source=index.source,
            runtime_source=runtime_source,
            index=index,
            member_function_sources=member_function_sources,
        )
        metadata_yaml = _format_member_metadata_yaml(
            _member_metadata_for_cluster_refactor(ctx, members=exemplars)
        )
        return format_cluster_fingerprint_dump(
            summary,
            exemplar_keys_by_address=ctx.expected_member_keys,
            key_vocabulary_yaml=vocabulary_yaml,
            member_metadata_yaml=metadata_yaml,
            dependency_stubs=dependency_stubs,
            helper_name=ctx.expected_helper_name,
        )

    prompt_members = sample_members_for_prompt(ctx.members, limit=member_limit)
    member_function_sources = {
        member.function_name: member.python_source for member in prompt_members
    }
    return build_cluster_refactor_context_dump(
        member_function_names=tuple(member.function_name for member in prompt_members),
        internals_source=index.source,
        runtime_source=runtime_source,
        key_vocabulary=filtered_vocabulary,
        member_metadata=_member_metadata_for_cluster_refactor(
            ctx, members=prompt_members
        ),
        shown_member_count=len(prompt_members),
        total_member_count=len(ctx.members),
        index=index,
        member_function_sources=member_function_sources,
    )


def _type_hint_runtime_imports(source: str) -> set[str]:
    """Collect allowlisted type-hint names that appear in refactored source."""
    return {name for name in ALLOWED_REFACTOR_TYPE_HINT_NAMES if name in source}


def _missing_runtime_imports(source: str, symbols: set[str]) -> set[str]:
    """Return ``symbols`` not already named on the ``from .runtime import`` line."""
    if not symbols:
        return set()
    paren = re.search(
        r"^from \.runtime import \(([^)]*)\)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if paren is not None:
        imported = paren.group(1)
    else:
        flat = re.search(r"^from \.runtime import (.+)$", source, re.MULTILINE)
        if flat is None:
            return set(symbols)
        imported = flat.group(1)
    existing = {part.strip() for part in imported.split(",") if part.strip()}
    return symbols - existing


def _helper_memo_runtime_imports(helper_source: str) -> set[str]:
    """Collect ``xl_memoize`` / ``xl_helper`` when referenced by a helper body."""
    needed: set[str] = set()
    if re.search(r"\bxl_memoize\b", helper_source):
        needed.add("xl_memoize")
    if re.search(r"\bxl_helper\b", helper_source):
        needed.add("xl_helper")
    return needed


def _runtime_module_symbols(package_root: Path | None = None) -> frozenset[str]:
    """Names exported by the packaged ``runtime`` module.

    Empty when ``package_root`` is omitted or the runtime file is missing
    (isolated tooling and unit tests), which leaves the type-hint and
    memoization scans as the only contributors — the pre-existing behaviour.
    """
    if package_root is None:
        return frozenset()
    try:
        return frozenset(allowed_runtime_module_symbols(package_root))
    except OSError:
        return frozenset()


def _bound_names(tree: ast.AST) -> set[str]:
    """Names ``tree`` binds itself (assignments, parameters, defs, imports)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".", maxsplit=1)[0])
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            bound.add(node.name)
    return bound


def _free_names(source: str) -> set[str]:
    """Names ``source`` reads without binding them, so they must be imported."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", source))
    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return loaded - _bound_names(tree)


def _referenced_runtime_imports(
    source: str,
    *,
    package_root: Path | None = None,
) -> set[str]:
    """Collect every runtime-exported symbol a refactored body actually calls.

    Refactored bodies may introduce runtime calls the pristine module never
    made (``xl_cell`` for reads that stay raw address reads, for example), so
    the import bundle has to follow the body rather than a fixed symbol list.
    """
    candidates = _runtime_module_symbols(package_root)
    if not candidates:
        return set()
    return set(candidates & _free_names(source))


def _refactored_body_runtime_imports(
    source: str,
    *,
    package_root: Path | None = None,
) -> set[str]:
    """Runtime symbols a refactored helper/singleton body needs imported."""
    return (
        _type_hint_runtime_imports(source)
        | _helper_memo_runtime_imports(source)
        | _referenced_runtime_imports(source, package_root=package_root)
    )


def _cluster_runtime_imports(
    response: ClusterRefactorResponse,
    *,
    package_root: Path | None = None,
) -> set[str]:
    return _refactored_body_runtime_imports(
        response.helper_source, package_root=package_root
    )


def ensure_cluster_refactor_imports(
    source: str,
    response: ClusterRefactorResponse,
    *,
    package_root: Path | None = None,
) -> str:
    needed = _cluster_runtime_imports(response, package_root=package_root)
    missing = _missing_runtime_imports(source, needed)
    if not missing:
        return source
    return _merge_runtime_imports(source, needed)


def _singleton_runtime_imports(
    response: SingletonRefactorResponse,
    *,
    package_root: Path | None = None,
) -> set[str]:
    return _refactored_body_runtime_imports(
        response.symbol_source, package_root=package_root
    )


def _merge_runtime_imports(source: str, symbols: set[str]) -> str:
    module = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in module.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "runtime" or node.level != 1:
            continue
        existing = {alias.name for alias in node.names}
        merged = sorted(existing | symbols)
        replacement = "from .runtime import " + ", ".join(merged) + "\n"
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        return "".join(lines[:start]) + replacement + "".join(lines[end:])
    return source


def ensure_singleton_refactor_imports(
    source: str,
    response: SingletonRefactorResponse,
    *,
    package_root: Path | None = None,
) -> str:
    needed = _singleton_runtime_imports(response, package_root=package_root)
    missing = _missing_runtime_imports(source, needed)
    if not missing:
        return source
    return _merge_runtime_imports(source, needed)


def singleton_prompt_payload(ctx: SingletonRefactorContext) -> dict[str, object]:
    return {
        "address": ctx.address,
        "function_name": ctx.function_name,
        "canonical_template": ctx.canonical_template,
        "normalized_formula": ctx.normalized_formula,
        "python_source": ctx.python_source,
        "dependency_addresses": list(ctx.dependency_addresses),
        "external_dependencies": list(ctx.external_dependencies),
        "semantic_dependencies": [
            {
                "address_template": dependency.address_template,
                "columns": list(dependency.columns),
                "helper_name": dependency.helper_name,
                "call_form": dependency.call_form,
            }
            for dependency in ctx.semantic_dependencies
        ],
        "call_sites": [
            {
                "caller_function": site.caller_function,
                "caller_address": site.caller_address,
                "callee_function": site.callee_function,
                "callee_address": site.callee_address,
                "pattern": site.pattern,
                "line": site.line,
                "snippet": site.snippet,
            }
            for site in ctx.call_sites
        ],
        "constraints": {
            "allowed_runtime_symbols": list(ctx.allowed_runtime_symbols),
            "signature": "(ctx)",
            "preserve_semantics": True,
            "no_algebraic_simplification": True,
            "docstring_style": "google",
            "require_semantic_locals": True,
        },
        "naming_hints": ctx.naming_hints,
        "expected_helper_name": ctx.expected_helper_name,
    }


def validate_singleton_refactor_response(
    ctx: SingletonRefactorContext,
    response: SingletonRefactorResponse,
    *,
    existing_names: frozenset[str],
    internals_source: str,
    require_semantic_locals: bool = True,
) -> None:
    if response.symbol_name != ctx.expected_helper_name:
        raise ValueError(
            "symbol_name must equal locked helper name "
            f"{ctx.expected_helper_name!r}, got {response.symbol_name!r}"
        )
    validate_semantic_identifier(
        response.symbol_name,
        existing_names=existing_names,
        allow_name=ctx.expected_helper_name,
    )

    if not response.symbol_name.isidentifier():
        raise ValueError(
            f"symbol_name is not a valid identifier: {response.symbol_name!r}"
        )

    symbol_module = ast.parse(response.symbol_source)
    function_defs = [
        node for node in symbol_module.body if isinstance(node, ast.FunctionDef)
    ]
    if len(function_defs) != 1:
        raise ValueError("symbol_source must contain exactly one FunctionDef")
    symbol_def = function_defs[0]
    if symbol_def.name != response.symbol_name:
        raise ValueError(
            "symbol_source function name must match symbol_name "
            f"({response.symbol_name!r})"
        )

    source_docstring = _function_docstring(symbol_def)
    if source_docstring is None:
        raise ValueError("symbol_source must include a docstring")
    validate_google_style_docstring(source_docstring)

    if require_semantic_locals:
        validate_semantic_local_names(symbol_def)
    validate_no_cell_function_references(symbol_def)
    validate_no_xl_index_ref_of_xl_range(symbol_def)

    arg_names = [arg.arg for arg in symbol_def.args.args]
    if arg_names != ["ctx"]:
        raise ValueError(
            f"singleton must accept exactly (ctx); got ({', '.join(arg_names)})"
        )

    if any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        for node in ast.walk(symbol_module)
    ):
        raise ValueError("symbol_source must not contain imports")

    allowed_names = _without_cell_function_names(
        set(ctx.allowed_runtime_symbols)
        | set(ctx.external_dependencies)
        | {ctx.function_name}
        | {response.symbol_name}
        | semantic_helpers_available_for_calls(internals_source, existing_names)
        | set(ALLOWED_REFACTOR_TYPE_HINT_NAMES)
        | {"ctx"}
        | {"xl_helper", "xl_memoize"}
    )
    builtin_names = set(dir(builtins))
    for node in ast.walk(symbol_def):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in builtin_names or name in allowed_names:
            continue
        raise ValueError(
            f"symbol_source calls disallowed function {name!r}; "
            f"allowed: {sorted(allowed_names)}"
        )

    validate_allowed_global_references(symbol_def, allowed_names=allowed_names)


def _unify_peel_split_entrypoints(
    source: str,
    *,
    scheduled_helper_by_address: Mapping[str, str],
    address_to_series_id: Mapping[str, str],
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None,
) -> str:
    """Make each peel-split published series' base helper span the full range (#143).

    Derives every scheduled address's dispatch-key value (the projection period)
    from ``bound_address_keys`` and delegates sibling-owned years from the base
    (``series_id``) helper to the owning ``series_id_2`` sibling, so the api-layer
    ``compute_*`` loop -- which only ever calls the bare series id -- no longer runs
    the base regime for years it never covered. A no-op when binding keys are
    unavailable or nothing was peeled.
    """
    if not bound_address_keys:
        return source
    address_time_periods: dict[str, int] = {}
    for address in scheduled_helper_by_address:
        keys = bound_address_keys.get(address)
        if not keys:
            continue
        period = keys.get("TIME_PERIOD")
        if isinstance(period, int):
            address_time_periods[address] = period
    if not address_time_periods:
        return source
    updated, rewritten = inject_peel_entrypoint_dispatch(
        source,
        scheduled_helper_by_address=scheduled_helper_by_address,
        address_to_series_id=address_to_series_id,
        address_time_periods=address_time_periods,
    )
    if rewritten:
        logger.info(
            "pass1 peel entry-point unify: %d base helper(s) now dispatch to "
            "sibling peels: %s",
            len(rewritten),
            ", ".join(rewritten),
        )
    return updated


def apply_singleton_refactor_plan(
    source: str,
    response: SingletonRefactorResponse,
    ctx: SingletonRefactorContext,
) -> tuple[str, int]:
    source = ensure_singleton_refactor_imports(
        source, response, package_root=ctx.package_root
    )
    binding = CollapseBinding(
        address=ctx.address,
        function_name=ctx.function_name,
        helper_name=response.symbol_name,
        literal_call=f"{response.symbol_name}(ctx)",
    )
    module = ast.parse(source)
    replacement_span = _function_def_char_span(source, module, ctx.function_name)
    if replacement_span is None:
        raise KeyError(f"Function {ctx.function_name!r} not found")
    call_replacements = _collect_collapse_binding_replacements(
        source,
        module,
        (binding,),
    )
    replace_start, replace_end = replacement_span
    external_replacements = [
        edit
        for edit in call_replacements
        if not (replace_start <= edit[0] and edit[1] <= replace_end)
    ]
    edits: list[tuple[int, int, str]] = [
        (replace_start, replace_end, response.symbol_source.strip() + "\n\n"),
        *external_replacements,
    ]
    updated = _apply_char_span_edits(source, edits)
    if RESOLVER_SECTION_MARKER in updated:
        symbol_dispatch = _parse_symbol_dispatch(source, module=module)
        symbol_dispatch[ctx.address] = response.symbol_name
        updated = _replace_resolver_section(
            updated,
            _parse_address_dispatch(source, module=module) or {},
            symbol_dispatch=symbol_dispatch,
        )
    return updated, len(external_replacements)


def apply_refactor_plan(
    source: str,
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext | None = None,
) -> str:
    updated, _rewrite_count = apply_cluster_collapse(source, response, ctx)
    return updated


def collapse_bindings_for_response(
    response: ClusterRefactorResponse,
) -> tuple[CollapseBinding, ...]:
    parameter_pairs = tuple(
        (parameter.name, parameter.dimension_id) for parameter in response.parameters
    )
    return tuple(
        CollapseBinding(
            address=entry.address,
            function_name=entry.function_name,
            helper_name=response.helper_name,
            literal_call=render_literal_helper_call(
                response.helper_name,
                parameter_pairs,
                entry.keys_dict(),
            ),
        )
        for entry in response.member_keys
    )


def apply_cluster_collapse(
    source: str,
    response: ClusterRefactorResponse,
    ctx: ClusterRefactorContext | None = None,
) -> tuple[str, int]:
    return apply_cluster_collapses_batch(source, (response,), ctx=ctx)


def apply_cluster_collapses_batch(
    source: str,
    responses: Sequence[ClusterRefactorResponse],
    ctx: ClusterRefactorContext | None = None,
) -> tuple[str, int]:
    """Collapse one or more independent clusters with a single full-module parse.

    Helpers are inserted first (preserving prior single-collapse semantics so
    wrapper call sites inside newly inserted helpers are rewritten), then bindings
    and wrapper removals run against one shared AST.

    Runtime import merges for the symbols the helper bodies reference (type
    hints such as ``EvalContext``/``CellValue`` plus runtime callables such as
    ``xl_cell``) are applied once for the whole batch before inserts, and
    skipped entirely when the symbols are already imported, so a typed helper
    batch does not re-parse the module once per response.
    """
    package_root = ctx.package_root if ctx is not None else None
    if not responses:
        return source, 0
    needed_imports: set[str] = set()
    for response in responses:
        needed_imports |= _cluster_runtime_imports(response, package_root=package_root)
    missing_imports = _missing_runtime_imports(source, needed_imports)
    updated = (
        _merge_runtime_imports(source, needed_imports) if missing_imports else source
    )
    # Insert in reverse so the first response's helper ends closest to the
    # formula-section marker (stable, readable order matching schedule order).
    for response in reversed(tuple(responses)):
        updated = insert_helper_source(updated, response.helper_source)
    bindings = tuple(
        binding
        for response in responses
        for binding in collapse_bindings_for_response(response)
    )
    collapsed_functions = frozenset(
        entry.function_name for response in responses for entry in response.member_keys
    )
    # One full-module parse covers binding rewrites, wrapper removal, and
    # resolver-dispatch reads. String edits invalidate line numbers, so do not
    # re-parse afterward — dispatch values are taken from this AST.
    tree = ast.parse(updated)
    updated, rewrite_count = _apply_bindings_and_remove_functions(
        updated,
        bindings,
        collapsed_functions,
        module=tree,
    )
    dispatch_updates: AddressDispatch = {}
    for response in responses:
        dispatch_updates.update(_dispatch_entries_for_collapse(response))
    if dispatch_updates and RESOLVER_SECTION_MARKER in updated:
        dispatch = _parse_address_dispatch(updated, module=tree) or {}
        dispatch.update(dispatch_updates)
        updated = _replace_resolver_section(
            updated,
            dispatch,
            symbol_dispatch=_parse_symbol_dispatch(updated, module=tree),
        )
    return updated, rewrite_count


def _parameter_literals(
    parameters: tuple[HelperParameter, ...],
    keys: dict[str, BindingKeyValue],
) -> dict[str, BindingKeyValue]:
    return {parameter.name: keys[parameter.dimension_id] for parameter in parameters}


def _dispatch_entries_for_collapse(
    response: ClusterRefactorResponse,
) -> AddressDispatch:
    """Route every collapsed member address to its helper call in the resolver."""
    return {
        entry.address: (
            response.helper_name,
            _parameter_literals(response.parameters, entry.keys_dict()),
        )
        for entry in response.member_keys
    }


def _line_start_offsets(source: str) -> list[int]:
    starts = [0]
    for index, character in enumerate(source):
        if character == "\n":
            starts.append(index + 1)
    return starts


def _function_def_char_span(
    source: str,
    module: ast.Module,
    function_name: str,
) -> tuple[int, int] | None:
    lines = source.splitlines(keepends=True)
    line_starts = _line_start_offsets(source)
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        start = line_starts[node.lineno - 1]
        end_line = node.end_lineno or node.lineno
        while end_line < len(lines) and lines[end_line].strip() == "":
            end_line += 1
        end = line_starts[end_line] if end_line < len(lines) else len(source)
        return start, end
    return None


def _function_removal_char_spans(
    source: str,
    module: ast.Module,
    function_names: frozenset[str],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for name in function_names:
        span = _function_def_char_span(source, module, name)
        if span is not None:
            spans.append(span)
    return spans


def _collect_collapse_binding_replacements(
    source: str,
    module: ast.Module,
    bindings: tuple[CollapseBinding, ...],
) -> list[tuple[int, int, str]]:
    if not bindings:
        return []
    bindings_by_function = {binding.function_name: binding for binding in bindings}
    line_starts = _line_start_offsets(source)
    replacements: list[tuple[int, int, str]] = []
    visitor = _CollapseBindingsRewriteVisitor(
        bindings_by_function=bindings_by_function,
        line_starts=line_starts,
        replacements=replacements,
    )
    for function_def in _iter_function_defs(module.body):
        visitor.visit(function_def)
    return replacements


def _apply_char_span_edits(
    source: str,
    edits: list[tuple[int, int, str]],
) -> str:
    if not edits:
        return source
    updated = source
    for start, end, new_text in sorted(edits, key=lambda item: item[0], reverse=True):
        updated = updated[:start] + new_text + updated[end:]
    return updated


def _apply_bindings_and_remove_functions(
    source: str,
    bindings: tuple[CollapseBinding, ...],
    remove_names: frozenset[str],
    *,
    module: ast.Module,
) -> tuple[str, int]:
    """Rewrite collapse call sites and drop wrappers from one parsed module."""
    replacements = _collect_collapse_binding_replacements(source, module, bindings)
    deletions = _function_removal_char_spans(source, module, remove_names)

    def _subsumed(start: int, end: int) -> bool:
        return any(
            delete_start <= start and end <= delete_end
            for delete_start, delete_end in deletions
        )

    edits: list[tuple[int, int, str]] = [
        (start, end, new_text)
        for start, end, new_text in replacements
        if not _subsumed(start, end)
    ]
    edits.extend((start, end, "") for start, end in deletions)
    return _apply_char_span_edits(source, edits), len(replacements)


def substitute_collapse_bindings(
    source: str,
    bindings: tuple[CollapseBinding, ...],
    *,
    module: ast.Module | None = None,
) -> tuple[str, int]:
    if not bindings:
        return source, 0
    tree = module if module is not None else ast.parse(source)
    replacements = _collect_collapse_binding_replacements(source, tree, bindings)
    if not replacements:
        return source, 0
    return _apply_char_span_edits(source, replacements), len(replacements)


def collect_static_cell_function_references(source: str) -> frozenset[str]:
    module = ast.parse(source)
    references: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "xl_eval"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Name)
        ):
            references.add(node.args[2].id)
        if isinstance(node.func, ast.Name) and node.func.id.startswith("cell_"):
            references.add(node.func.id)
    return frozenset(references)


def parse_thin_literal_wrapper(
    function_def: ast.FunctionDef,
) -> tuple[str, dict[str, BindingKeyValue]] | None:
    """Return ``(helper_name, key_kwargs)`` for a one-line semantic helper wrapper."""
    return _parse_thin_helper_return(function_def)


def _parse_thin_helper_return(
    function_def: ast.FunctionDef,
) -> tuple[str, dict[str, BindingKeyValue]] | None:
    body = function_def.body
    start = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        start = 1
    executable_body = body[start:]
    if len(executable_body) != 1:
        return None
    statement = executable_body[0]
    if not isinstance(statement, ast.Return) or statement.value is None:
        return None
    call = statement.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "ctx"
    ):
        return None
    if call.keywords:
        key_kwargs: dict[str, BindingKeyValue] = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                return None
            if not isinstance(keyword.value, ast.Constant):
                return None
            literal = keyword.value.value
            if not isinstance(literal, (str, int, float, bool)):
                return None
            key_kwargs[keyword.arg] = literal
        return call.func.id, key_kwargs
    return None


_SHEET_ADDRESS_PATTERN = re.compile(
    r"^(?P<sheet>.+!)(?P<column>[A-Za-z]+)(?P<row>\d+)$"
)


def _column_address_template(address: str) -> str:
    match = _SHEET_ADDRESS_PATTERN.match(address)
    if match is None:
        return address
    return f"{match.group('sheet')}{{col}}{match.group('row')}"


def resolve_semantic_dependencies(
    source: str,
    dependency_addresses: Iterable[str],
    *,
    index: InternalsSourceIndex | None = None,
    address_to_series_id: Mapping[str, str] | None = None,
    address_to_helper_name: Mapping[str, str] | None = None,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None = None,
) -> tuple[tuple[SemanticDependency, ...], tuple[str, ...]]:
    """Resolve external ``cell_*`` dependencies to the semantic helpers wrapping them.

    A dependency whose per-cell ``cell_*`` function is still defined has not been
    collapsed yet, so it is reported unresolved.

    ``address_to_series_id`` lets a collapsed dependency resolve by its series id
    (which equals its helper name) when its per-cell wrapper is gone and the
    dispatch / docstring metadata cannot recover it — the stranded
    identity-passthrough case that otherwise drops to ``slots_without_read_sites``.

    ``address_to_helper_name`` maps each scheduled address to the helper name its
    refactor unit was allocated. A peel-split series publishes several helpers
    that all carry the same series id, so this is the only exact way to route an
    operand that crosses a peel boundary (issue #138).

    ``bound_address_keys`` bounds both routes: a helper whose literal key tables
    or ``raise``-terminated dispatch chain provably cannot serve the dependency's
    key is refused, so a multi-regime series never routes an out-of-regime year
    into a partial helper (#139).
    """
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    defined_functions = resolved.functions
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    unresolved: set[str] = set()
    for address in dependency_addresses:
        function_name = address_to_function_name(address)
        if function_name in defined_functions:
            unresolved.add(function_name)
            continue

        collapsed = _infer_collapsed_semantic_dependency(
            source,
            address,
            index=resolved,
            address_to_series_id=address_to_series_id,
            address_to_helper_name=address_to_helper_name,
            bound_address_keys=bound_address_keys,
        )
        if collapsed is None:
            unresolved.add(function_name)
            continue
        helper_name, parameter_name = collapsed
        grouped[helper_name].append((address, parameter_name))

    semantic_dependencies = tuple(
        SemanticDependency(
            helper_name=helper_name,
            call_form=_helper_pass_through_call_form(
                source, helper_name, index=resolved
            ),
            address_template=_column_address_template(min(entries)[0]),
            columns=tuple(tag for _, tag in sorted(entries)),
            addresses=tuple(address for address, _ in sorted(entries)),
        )
        for helper_name, entries in sorted(grouped.items())
    )
    return semantic_dependencies, tuple(sorted(unresolved))


def _helper_pass_through_call_form(
    source: str,
    helper_name: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> str:
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    node = resolved.functions.get(helper_name)
    if node is not None:
        parameter_names = [arg.arg for arg in node.args.args if arg.arg != "ctx"]
        if len(parameter_names) == 1:
            parameter_name = parameter_names[0]
            return f"{helper_name}(ctx, {parameter_name}={parameter_name})"
        return f"{helper_name}(ctx)"
    return f"{helper_name}(ctx)"


def _column_index(column: str) -> int:
    value = 0
    for character in column.upper():
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def _covered_addresses_text(docstring: str) -> str:
    """Return only the ``Covers ...`` clause of a helper docstring's Note.

    The ``Excel:`` formula transcription is excluded so that addresses appearing
    inside a formula (which the helper reads, but does not compute) are never
    mistaken for cells the helper covers.
    """
    normalized = docstring.replace("$", "")
    covers_index = normalized.find("Covers")
    if covers_index == -1:
        return ""
    covered = normalized[covers_index + len("Covers") :]
    excel_index = covered.find("Excel:")
    if excel_index != -1:
        covered = covered[:excel_index]
    return covered


def _address_in_docstring_range(docstring: str, address: str) -> bool:
    covered = _covered_addresses_text(docstring)
    if not covered:
        return False
    if address.replace("$", "") in covered:
        return True
    match = _SHEET_ADDRESS_PATTERN.match(address)
    if match is None:
        return False
    sheet = match.group("sheet")
    column = match.group("column").upper()
    row = match.group("row")
    column_index = _column_index(column)
    range_pattern = re.compile(
        rf"{re.escape(sheet)}(?P<start>[A-Z]+){row}:(?P<end>[A-Z]+){row}"
    )
    for range_match in range_pattern.finditer(covered):
        start_index = _column_index(range_match.group("start"))
        end_index = _column_index(range_match.group("end"))
        if start_index <= column_index <= end_index:
            return True
    return False


def _constant_key_values(node: ast.expr) -> tuple[BindingKeyValue, ...] | None:
    """Return the constant members of a literal collection, or ``None``.

    ``None`` means the literal is not made entirely of hashable constants, so no
    key domain can be proven from it.
    """
    if isinstance(node, ast.Dict):
        elements: list[ast.expr | None] = list(node.keys)
    elif isinstance(node, ast.Set | ast.Tuple | ast.List):
        elements = list(node.elts)
    else:
        return None
    values: list[BindingKeyValue] = []
    for element in elements:
        if not isinstance(element, ast.Constant):
            return None
        value = element.value
        if not isinstance(value, str | int | float | bool):
            return None
        values.append(value)
    return tuple(values)


def _literal_table_domains(
    helper_def: ast.FunctionDef, parameter_name: str
) -> frozenset[BindingKeyValue] | None:
    """Key domain implied by literal lookup tables indexed by ``parameter_name``.

    Only *unconditional* top-level statements are considered: a table read inside
    a branch is reached by some keys and skipped by others, so it proves nothing
    on its own. Likewise only a direct ``table[parameter]`` subscript counts — an
    offset read such as ``table[time_period - 1]`` needs a different key than the
    one the caller passes.
    """
    unconditional = [
        statement for statement in helper_def.body if not isinstance(statement, ast.If)
    ]
    tables: dict[str, frozenset[BindingKeyValue]] = {}
    for statement in unconditional:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        keys = _constant_key_values(statement.value)
        if keys is None:
            continue
        tables[target.id] = frozenset(keys)

    domains: list[frozenset[BindingKeyValue]] = []
    for statement in unconditional:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Subscript):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            table = tables.get(node.value.id)
            if table is None:
                continue
            if isinstance(node.slice, ast.Name) and node.slice.id == parameter_name:
                domains.append(table)
    if not domains:
        return None
    # Union, not intersection: a key only has to be in one of the tables reached
    # on its path, and over-rejecting would strand dependencies the #134 fallback
    # exists to rescue.
    return frozenset().union(*domains)


def _dispatch_branch_values(
    test: ast.expr, parameter_name: str
) -> tuple[BindingKeyValue, ...] | None:
    """Key values selected by ``if <param> == k`` / ``if <param> in {..}``."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    left = test.left
    if not isinstance(left, ast.Name) or left.id != parameter_name:
        return None
    operator = test.ops[0]
    comparator = test.comparators[0]
    if isinstance(operator, ast.Eq):
        if not isinstance(comparator, ast.Constant) or not isinstance(
            comparator.value, str | int | float | bool
        ):
            return None
        return (comparator.value,)
    if isinstance(operator, ast.In):
        return _constant_key_values(comparator)
    return None


def _top_level_guarded_values(
    helper_def: ast.FunctionDef, parameter_name: str
) -> tuple[frozenset[BindingKeyValue], bool]:
    """Keys selected by top-level ``if <param> == k`` / ``in {..}`` branches.

    The second element is ``False`` when a top-level ``if`` branches on something
    other than ``parameter_name``, which means keys the guards do not name may
    still be served by that branch.
    """
    values: list[BindingKeyValue] = []
    guards_are_exhaustive = True
    for statement in helper_def.body:
        if not isinstance(statement, ast.If):
            continue
        branch_values = _dispatch_branch_values(statement.test, parameter_name)
        if branch_values is None:
            guards_are_exhaustive = False
            continue
        values.extend(branch_values)
    return frozenset(values), guards_are_exhaustive


def helper_static_key_domain(
    helper_def: ast.FunctionDef, parameter_name: str
) -> frozenset[BindingKeyValue] | None:
    """Return the key values ``helper_def`` can provably serve, or ``None``.

    ``None`` means unbounded as far as static analysis can tell. A non-``None``
    domain means every other key hard-fails inside the helper — a literal lookup
    table raises ``KeyError``, and a ``raise``-terminated dispatch chain (the
    shape ``synthesize_key_dispatch_body`` emits) raises ``ValueError`` — so
    routing such a key there emits code that cannot run.

    Keys picked off by a top-level ``if <param> == k`` guard count as served even
    when the fall-through body's lookup tables omit them, so a special-cased
    period is never reported as out of domain. A top-level branch on anything
    else can serve keys this analysis cannot enumerate, so it forfeits the
    domain entirely rather than guess.
    """
    if not parameter_name:
        return None
    guarded, guards_are_exhaustive = _top_level_guarded_values(
        helper_def, parameter_name
    )
    if not guards_are_exhaustive:
        return None
    table_domain = _literal_table_domains(helper_def, parameter_name)
    if table_domain is not None:
        return table_domain | guarded
    body = [
        statement
        for statement in helper_def.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
        )
    ]
    ends_in_raise = bool(body) and isinstance(body[-1], ast.Raise)
    if ends_in_raise and guarded:
        return guarded
    return None


def validate_helper_serves_member_keys(
    helper_def: ast.FunctionDef,
    *,
    helper_name: str,
    parameter_names_by_dimension: Mapping[str, str],
    member_keys_by_address: Mapping[str, Mapping[str, BindingKeyValue]],
) -> None:
    """Reject a helper that cannot serve a key of a member it claims.

    A multi-regime series (``Baseline!D12:CP12`` labour productivity growth) can
    be refactored into a helper that implements one regime while still claiming
    every member. The claimed keys outside the implemented regime then hard-fail
    at evaluation — ``KeyError`` on a literal period table, ``ValueError`` on a
    key-dispatch chain — so the mismatch has to be caught before the helper lands
    (#139). Regimes belong in separate helpers or in explicit branches.
    """
    for dimension_id, parameter_name in sorted(parameter_names_by_dimension.items()):
        domain = helper_static_key_domain(helper_def, parameter_name)
        if domain is None:
            continue
        unserved = {
            keys[dimension_id]
            for keys in member_keys_by_address.values()
            if dimension_id in keys and keys[dimension_id] not in domain
        }
        if not unserved:
            continue
        raise ValueError(
            f"helper {helper_name!r} cannot serve member keys "
            f"{sorted(unserved, key=repr)} on {dimension_id}: its lookup tables "
            f"and key branches only cover {sorted(domain, key=repr)}"
        )


def _series_helper_serves_address(
    resolved: InternalsSourceIndex,
    helper_name: str,
    parameter_name: str,
    address: str,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None,
) -> bool:
    """Whether ``helper_name`` can serve ``address``'s bound key.

    Only a *provable* mismatch returns ``False``; missing keys or an unbounded
    helper keep the permissive #134 behaviour.
    """
    if not bound_address_keys or not parameter_name:
        return True
    keys = bound_address_keys.get(address)
    if not keys:
        return True
    key_values = [
        value
        for dimension_id, value in keys.items()
        if dimension_id_to_param_name(dimension_id) == parameter_name
    ]
    if len(key_values) != 1:
        return True
    helper_def = resolved.functions.get(helper_name)
    if helper_def is None:
        return True
    domain = helper_static_key_domain(helper_def, parameter_name)
    if domain is None:
        return True
    return key_values[0] in domain


def _sole_helper_parameter(resolved: InternalsSourceIndex, helper_name: str) -> str:
    """Return the helper's single non-``ctx`` parameter name, or ``""``."""
    helper_def = resolved.functions.get(helper_name)
    if helper_def is None:
        return ""
    parameter_names = [arg.arg for arg in helper_def.args.args if arg.arg != "ctx"]
    return parameter_names[0] if len(parameter_names) == 1 else ""


def _infer_collapsed_from_dispatch(
    resolved: InternalsSourceIndex, address: str
) -> tuple[str, str] | None | Literal[False]:
    """Recover ``(helper_name, parameter)`` from the generated dispatch tables.

    ``None`` means no dispatch entry claims the address (try the other
    heuristics); ``False`` means one does but names something that is not a live
    semantic helper, which stops resolution as it always has.
    """
    dispatch = resolved.address_dispatch
    if address in dispatch:
        helper_name, key_kwargs = dispatch[address]
        if helper_name not in resolved.semantic_helper_names:
            return False
        if len(key_kwargs) == 1:
            return helper_name, next(iter(key_kwargs))
        return helper_name, next(iter(key_kwargs))

    symbol_dispatch = resolved.symbol_dispatch
    symbol_name = symbol_dispatch.get(address)
    if symbol_name is not None:
        if symbol_name not in resolved.semantic_helper_names:
            return False
        return symbol_name, ""
    return None


def _infer_collapsed_from_docstrings(
    resolved: InternalsSourceIndex, address: str
) -> tuple[str, str] | None:
    """Recover ``(helper_name, parameter)`` from advertised docstring coverage."""
    defined_functions = resolved.functions
    matches: list[tuple[str, str]] = []
    for helper_name in sorted(resolved.semantic_helper_names):
        helper_def = defined_functions.get(helper_name)
        if helper_def is None:
            continue
        docstring = _function_docstring(helper_def)
        if docstring is None or not _address_in_docstring_range(docstring, address):
            continue
        parameter_names = [arg.arg for arg in helper_def.args.args if arg.arg != "ctx"]
        if len(parameter_names) == 1:
            matches.append((helper_name, parameter_names[0]))
        elif not parameter_names:
            matches.append((helper_name, ""))
    if len(matches) == 1:
        return matches[0]
    return None


def _infer_collapsed_semantic_dependency(
    source: str,
    address: str,
    *,
    index: InternalsSourceIndex | None = None,
    address_to_series_id: Mapping[str, str] | None = None,
    address_to_helper_name: Mapping[str, str] | None = None,
    bound_address_keys: Mapping[str, Mapping[str, BindingKeyValue]] | None = None,
) -> tuple[str, str] | None:
    resolved = index if index is not None else InternalsSourceIndex.from_source(source)
    dispatched = _infer_collapsed_from_dispatch(resolved, address)
    if dispatched is False:
        return None
    if dispatched is not None:
        return dispatched

    # The schedule locks one helper name per refactor unit before the pass
    # (``allocate_schedule_helper_names``), so a peel-split series has an exact
    # owner per address: the base series id for one unit, ``series_id_2`` for the
    # next (issue #138). Prefer that owner over the coverage heuristics below,
    # which cannot tell the peels apart and would route a later-peel address to
    # the earlier peel's helper. Only trust it while it names a live helper: a
    # unit that failed to refactor keeps its ``cell_*`` wrappers, and pass 2
    # renames helpers. A scheduled owner that provably cannot serve the address's
    # bound key is stale evidence, so it defers to the heuristics below (#139).
    if address_to_helper_name:
        scheduled_helper = address_to_helper_name.get(address)
        if (
            scheduled_helper is not None
            and scheduled_helper in resolved.semantic_helper_names
        ):
            parameter_name = _sole_helper_parameter(resolved, scheduled_helper)
            if _series_helper_serves_address(
                resolved,
                scheduled_helper,
                parameter_name,
                address,
                bound_address_keys,
            ):
                return scheduled_helper, parameter_name
            logger.info(
                "scheduled owner declined for %s: %s cannot serve its bound key",
                address,
                scheduled_helper,
            )

    inferred = _infer_collapsed_from_docstrings(resolved, address)
    if inferred is not None:
        return inferred

    # Fallback: helper names ARE series ids (``expected_helper_name =
    # sole_series_id_for_addresses``), so a collapsed dependency whose series id
    # is an existing semantic helper resolves even when its per-cell wrapper is
    # gone and the dispatch / docstring metadata cannot recover it. This rescues
    # stranded identity passthroughs (``=Baseline!AA33``) whose engine dependency
    # was refactored out from under them, which otherwise strand as unresolved
    # and drop the cluster to ``slots_without_read_sites`` / LLM fallback.
    #
    # The fallback carries no coverage evidence, so it must not route a key the
    # helper provably cannot serve (#139): a series whose regimes were split
    # across helpers (``Baseline!D12:CP12`` labour productivity) still maps every
    # address to the one series id, and resolving the out-of-regime years there
    # emits calls that raise ``KeyError`` on the helper's period tables.
    if address_to_series_id:
        series_id = address_to_series_id.get(address)
        if series_id is not None and series_id in resolved.semantic_helper_names:
            parameter_name = _sole_helper_parameter(resolved, series_id)
            if _series_helper_serves_address(
                resolved,
                series_id,
                parameter_name,
                address,
                bound_address_keys,
            ):
                return series_id, parameter_name
            logger.info(
                "series-id fallback declined for %s: %s cannot serve its bound key",
                address,
                series_id,
            )
    return None


def function_name_to_workbook_address(function_name: str) -> str | None:
    return _caller_address(function_name)


def _top_level_function_char_span(
    source: str,
    node: ast.FunctionDef,
    *,
    line_starts: list[int],
    lines: list[str],
) -> tuple[int, int]:
    """Return the ``[start, end)`` char span for a top-level function, including decorators."""
    start_line = node.lineno
    if node.decorator_list:
        start_line = min(decorator.lineno for decorator in node.decorator_list)
    start = line_starts[start_line - 1]
    end_line = node.end_lineno or node.lineno
    while end_line < len(lines) and lines[end_line].strip() == "":
        end_line += 1
    end = line_starts[end_line] if end_line < len(lines) else len(source)
    return start, end


def rehome_unrefactored_cell_functions(source: str) -> str:
    """Move residual ``cell_*`` defs into ``UNREFACTORED_CELLS_SECTION_MARKER``.

    Helpers stay under ``FORMULA_SECTION_MARKER``. Remaining ``cell_*``
    implementations — including those excel-grapher emitted under
    ``PROJECTION_ALIAS_SECTION_MARKER`` — are collected into a clearly labeled
    unrefactored section before the resolver. The projection-alias marker is
    dropped when that section is rebuilt.
    """
    has_formula = FORMULA_SECTION_MARKER in source
    has_resolver = RESOLVER_SECTION_MARKER in source
    if not has_formula and not has_resolver:
        # Synthetic fixtures and partial modules omit codegen section markers.
        return source
    if not has_formula:
        raise ValueError(f"Missing section marker {FORMULA_SECTION_MARKER!r}")
    if not has_resolver:
        raise ValueError(f"Missing section marker {RESOLVER_SECTION_MARKER!r}")

    formula_at = source.index(FORMULA_SECTION_MARKER)
    after_formula_marker = source.index("\n", formula_at) + 1
    resolver_at = source.index(RESOLVER_SECTION_MARKER)
    formula_line = source.count("\n", 0, formula_at) + 1
    resolver_line = source.count("\n", 0, resolver_at) + 1

    module = ast.parse(source)
    line_starts = _line_start_offsets(source)
    lines = source.splitlines(keepends=True)
    helpers: list[str] = []
    residuals: list[str] = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        start_line = node.lineno
        if node.decorator_list:
            start_line = min(decorator.lineno for decorator in node.decorator_list)
        if start_line <= formula_line or start_line >= resolver_line:
            continue
        start, end = _top_level_function_char_span(
            source,
            node,
            line_starts=line_starts,
            lines=lines,
        )
        text = source[start:end].rstrip() + "\n"
        if node.name.startswith("cell_"):
            residuals.append(text)
        else:
            helpers.append(text)

    parts: list[str] = [source[:after_formula_marker]]
    if helpers:
        parts.append("\n")
        parts.append("\n\n".join(helpers))
        parts.append("\n")
    if residuals:
        parts.append("\n")
        parts.append(UNREFACTORED_CELLS_SECTION_MARKER)
        parts.append("\n\n")
        parts.append("\n\n".join(residuals))
        parts.append("\n")
    parts.append("\n")
    parts.append(source[resolver_at:])
    return "".join(parts)


def apply_phase_c(source: str) -> tuple[str, int]:
    """Drop unreferenced thin ``cell_*`` wrappers and route them via ``_ADDRESS_DISPATCH``."""
    referenced = collect_static_cell_function_references(source)
    module = ast.parse(source)
    dispatch: AddressDispatch = {}
    to_prune: set[str] = set()

    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("cell_"):
            continue
        thin_wrapper = parse_thin_literal_wrapper(node)
        if thin_wrapper is None:
            continue
        if node.name in referenced:
            continue
        address = function_name_to_workbook_address(node.name)
        if address is None:
            continue
        helper_name, key_kwargs = thin_wrapper
        dispatch[address] = (helper_name, key_kwargs)
        to_prune.add(node.name)

    if not to_prune:
        return source, 0

    updated = _remove_function_definitions(source, frozenset(to_prune))
    existing_dispatch = _parse_address_dispatch(updated) or {}
    existing_dispatch.update(dispatch)
    updated = _replace_resolver_section(
        updated,
        existing_dispatch,
        symbol_dispatch=_parse_symbol_dispatch(source),
    )
    return updated, len(to_prune)


def _parse_address_dispatch(
    source: str,
    *,
    module: ast.Module | None = None,
) -> AddressDispatch | None:
    tree = module if module is not None else ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_ADDRESS_DISPATCH":
                if not isinstance(node.value, ast.Dict):
                    raise ValueError("_ADDRESS_DISPATCH must be a dict literal")
                dispatch: AddressDispatch = {}
                for key, value in zip(node.value.keys, node.value.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and isinstance(value, ast.Tuple)
                        and len(value.elts) == 2
                        and isinstance(value.elts[0], ast.Constant)
                        and isinstance(value.elts[0].value, str)
                        and isinstance(value.elts[1], ast.Dict)
                    ):
                        raise ValueError("_ADDRESS_DISPATCH has unexpected entry shape")
                    key_kwargs: dict[str, BindingKeyValue] = {}
                    for kw_key, kw_value in zip(
                        value.elts[1].keys,
                        value.elts[1].values,
                    ):
                        if not (
                            isinstance(kw_key, ast.Constant)
                            and isinstance(kw_key.value, str)
                            and isinstance(kw_value, ast.Constant)
                        ):
                            raise TypeError(
                                "_ADDRESS_DISPATCH keyword args must be constant literals"
                            )
                        literal = kw_value.value
                        if not isinstance(literal, (str, int, float, bool)):
                            raise TypeError(
                                "_ADDRESS_DISPATCH keyword args must be scalar literals"
                            )
                        key_kwargs[kw_key.value] = literal
                    dispatch[key.value] = (value.elts[0].value, key_kwargs)
                return dispatch
    return None


def _parse_symbol_dispatch(
    source: str,
    *,
    module: ast.Module | None = None,
) -> dict[str, str]:
    tree = module if module is not None else ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_SYMBOL_DISPATCH":
                if not isinstance(node.value, ast.Dict):
                    raise ValueError("_SYMBOL_DISPATCH must be a dict literal")
                dispatch: dict[str, str] = {}
                for key, value in zip(node.value.keys, node.value.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        raise TypeError("_SYMBOL_DISPATCH has unexpected entry shape")
                    dispatch[key.value] = value.value
                return dispatch
    return {}


def _remove_function_definitions(
    source: str,
    function_names: frozenset[str],
    *,
    module: ast.Module | None = None,
) -> str:
    tree = module if module is not None else ast.parse(source)
    spans = _function_removal_char_spans(source, tree, function_names)
    return _apply_char_span_edits(
        source,
        [(start, end, "") for start, end in spans],
    )


def _replace_resolver_section(
    source: str,
    address_dispatch: AddressDispatch,
    *,
    symbol_dispatch: dict[str, str] | None = None,
) -> str:
    if symbol_dispatch is None:
        symbol_dispatch = _parse_symbol_dispatch(source)
    if RESOLVER_SECTION_MARKER not in source:
        raise ValueError(f"Missing section marker {RESOLVER_SECTION_MARKER!r}")
    head = source[: source.index(RESOLVER_SECTION_MARKER)]
    return head + _render_resolver_section(address_dispatch, symbol_dispatch)


def _render_resolver_section(
    address_dispatch: AddressDispatch,
    symbol_dispatch: dict[str, str] | None = None,
) -> str:
    symbol_dispatch = symbol_dispatch or {}
    dispatch_lines: list[str] = []
    for address, (helper, key_kwargs) in sorted(address_dispatch.items()):
        kw_parts = ", ".join(
            f"{name!r}: {format_binding_key_literal(value)}"
            for name, value in sorted(key_kwargs.items())
        )
        dispatch_lines.append(f"    {address!r}: ({helper!r}, {{{kw_parts}}}),")
    dispatch_body = "{\n" + "\n".join(dispatch_lines) + "\n}"
    symbol_lines = [
        f"    {address!r}: {symbol!r},"
        for address, symbol in sorted(symbol_dispatch.items())
    ]
    symbol_body = "{\n" + "\n".join(symbol_lines) + "\n}" if symbol_lines else "{}"
    return f"""{RESOLVER_SECTION_MARKER}
_RESOLVED_FORMULAS = {{}}
_ADDRESS_DISPATCH = {dispatch_body}
_SYMBOL_DISPATCH = {symbol_body}

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
    return f"cell_{{base}}"

def _resolve_formula(address):
    fn = _RESOLVED_FORMULAS.get(address)
    if fn is not None:
        return fn
    dispatch = _ADDRESS_DISPATCH.get(address)
    if dispatch is not None:
        helper_name, key_kwargs = dispatch
        helper = globals()[helper_name]

        def _bound(ctx, _helper=helper, _key_kwargs=key_kwargs):
            return _helper(ctx, **_key_kwargs)

        _RESOLVED_FORMULAS[address] = _bound
        return _bound
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


def insert_helper_source(source: str, helper_source: str) -> str:
    helper_source = helper_source.strip()
    helper_module = ast.parse(helper_source)
    helper_defs = [
        node for node in helper_module.body if isinstance(node, ast.FunctionDef)
    ]
    if len(helper_defs) != 1:
        raise ValueError("helper_source must contain exactly one FunctionDef")
    helper_name = helper_defs[0].name
    if re.search(rf"^def {re.escape(helper_name)}\(", source, re.MULTILINE):
        raise ValueError(
            f"helper {helper_name!r} already exists; schedule allocation must "
            "assign a unique name rather than overwrite"
        )
    if FORMULA_SECTION_MARKER not in source:
        raise ValueError(f"Missing section marker {FORMULA_SECTION_MARKER!r}")

    marker_index = source.index(FORMULA_SECTION_MARKER)
    insert_at = source.index("\n", marker_index) + 1
    while insert_at < len(source) and source[insert_at] == "\n":
        insert_at += 1
    return source[:insert_at] + helper_source + "\n\n" + source[insert_at:]


def _xl_eval_address_arg(node: ast.Call) -> str | None:
    if len(node.args) < 2:
        return None
    address_arg = node.args[1]
    if isinstance(address_arg, ast.Constant) and isinstance(address_arg.value, str):
        return address_arg.value
    if isinstance(address_arg, ast.JoinedStr):
        parts: list[str] = []
        for value in address_arg.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{col}")
        return "".join(parts)
    return None


def _xl_eval_callee_function(node: ast.Call) -> str | None:
    if len(node.args) < 3:
        return None
    callee_arg = node.args[2]
    if isinstance(callee_arg, ast.Name):
        return callee_arg.id
    return None


def _xl_eval_matches_collapse(
    node: ast.Call,
    binding: CollapseBinding,
) -> bool:
    if not (isinstance(node.func, ast.Name) and node.func.id == "xl_eval"):
        return False

    address_pattern = _xl_eval_address_arg(node)
    if address_pattern is None:
        return False

    callee_function = _xl_eval_callee_function(node)
    if address_pattern == binding.address:
        return callee_function == binding.function_name

    if address_pattern == _column_address_template(binding.address):
        return callee_function == binding.function_name
    return False


class _CollapseBindingsRewriteVisitor(ast.NodeVisitor):
    """Rewrite direct and xl_eval call sites for every collapse binding in one walk."""

    def __init__(
        self,
        *,
        bindings_by_function: dict[str, CollapseBinding],
        line_starts: list[int],
        replacements: list[tuple[int, int, str]],
    ) -> None:
        self.bindings_by_function = bindings_by_function
        self.line_starts = line_starts
        self.replacements = replacements

    def visit_Call(self, node: ast.Call) -> None:
        binding: CollapseBinding | None = None
        if isinstance(node.func, ast.Name) and node.func.id == "xl_eval":
            callee = _xl_eval_callee_function(node)
            if callee is not None:
                candidate = self.bindings_by_function.get(callee)
                if candidate is not None and _xl_eval_matches_collapse(node, candidate):
                    binding = candidate
        elif (
            isinstance(node.func, ast.Name)
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "ctx"
        ):
            binding = self.bindings_by_function.get(node.func.id)

        if (
            binding is not None
            and node.end_lineno is not None
            and node.end_col_offset is not None
        ):
            start = self.line_starts[node.lineno - 1] + node.col_offset
            end = self.line_starts[node.end_lineno - 1] + node.end_col_offset
            self.replacements.append((start, end, binding.literal_call))
        self.generic_visit(node)


def _iter_function_defs(body: list[ast.stmt]) -> list[ast.FunctionDef]:
    return [node for node in body if isinstance(node, ast.FunctionDef)]


def validate_refactored_internals(source: str) -> None:
    module = ast.parse(source)
    seen_names: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name in seen_names:
            raise ValueError(f"duplicate top-level function definition: {node.name!r}")
        seen_names.add(node.name)
    compile(source, "internals.py", "exec")


def _record_cluster_name_delta(
    names: set[str],
    *,
    helper_name: str,
    removed_names: Iterable[str],
) -> None:
    """Update a live top-level def set after a cluster collapse (fail-fast duplicates)."""
    removed = set(removed_names)
    if helper_name in names and helper_name not in removed:
        raise ValueError(f"duplicate top-level function definition: {helper_name!r}")
    names -= removed
    names.add(helper_name)


def _record_singleton_name_delta(
    names: set[str],
    *,
    old_name: str,
    new_name: str,
) -> None:
    """Update a live top-level def set after a singleton rewrite."""
    if new_name != old_name and new_name in names:
        raise ValueError(f"duplicate top-level function definition: {new_name!r}")
    names.discard(old_name)
    names.add(new_name)


def refactor_internals_singleton(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    response: SingletonRefactorResponse | None = None,
    dry_run: bool = False,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
    flush: bool = True,
    apply_source: str | None = None,
    validate_module: bool = True,
) -> SingletonRefactorApplyResult:
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    apply_base = source if apply_source is None else apply_source
    existing_names = _function_names(source, index=index)
    if response is None:
        response = llm_refactor_singleton(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
            diagnostic_target=diagnostic_target,
            internals_index=index,
        )
    validate_singleton_refactor_response(
        ctx,
        response,
        existing_names=existing_names,
        internals_source=source,
    )
    updated, rewrite_count = apply_singleton_refactor_plan(apply_base, response, ctx)
    if validate_module:
        validate_refactored_internals(updated)
    if not dry_run and flush:
        internals_path.write_text(updated, encoding="utf-8", newline="\n")
    return SingletonRefactorApplyResult(
        source=updated,
        symbol_name=response.symbol_name,
        old_function_name=ctx.function_name,
        reference_rewrites=rewrite_count,
        dry_run=dry_run,
        response=response,
    )


def refactor_internals_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    response: ClusterRefactorResponse | None = None,
    dry_run: bool = False,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
    flush: bool = True,
    apply_source: str | None = None,
    validate_module: bool = True,
) -> ClusterRefactorApplyResult:
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    source = index.source
    apply_base = source if apply_source is None else apply_source
    existing_names = _function_names(source, index=index)
    if response is None:
        response = llm_refactor_cluster(
            ctx,
            internals_path=internals_path,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
            diagnostic_target=diagnostic_target,
            internals_index=index,
        )
    validate_cluster_refactor_response(
        ctx,
        response,
        existing_names=existing_names,
        internals_source=source,
    )
    updated = apply_refactor_plan(apply_base, response, ctx)
    if validate_module:
        validate_refactored_internals(updated)
    if not dry_run and flush:
        internals_path.write_text(updated, encoding="utf-8", newline="\n")
    return ClusterRefactorApplyResult(
        source=updated,
        helper_name=response.helper_name,
        wrappers_applied=tuple(entry.function_name for entry in response.member_keys),
        dry_run=dry_run,
        response=response,
    )


@dataclass
class _PendingSemanticUnit:
    """A mechanically refactored unit awaiting parallel LLM semantic naming."""

    kind: Literal["cluster", "singleton"]
    unit_id: str
    helper_name: str
    diagnostic_target: str
    canonical_template: str
    contract: str | None
    draft: MechanicalBodyDraft
    ctx: ClusterRefactorContext | SingletonRefactorContext
    parameter_names: frozenset[str]
    forbidden_names: frozenset[str]
    member_fingerprints: tuple[tuple[str, str, str], ...]
    member_checks: tuple[tuple[str, dict[str, BindingKeyValue]], ...]


@dataclass
class _PendingMechanicalClusterApply:
    """A validated mechanical cluster collapse waiting for a layer-batched apply."""

    cluster_members: tuple[str, ...]
    cluster_ctx: ClusterRefactorContext
    draft: MechanicalBodyDraft
    mechanical_response: ClusterRefactorResponse
    existing_names: frozenset[str]
    diagnostic_target: str
    timing_context_s: float = 0.0
    timing_synthesize_s: float = 0.0
    timing_reindex_s: float = 0.0
    timing_reindexed: bool = False


def _member_fingerprint(
    address: str, formula: str, source: str
) -> tuple[str, str, str]:
    return (
        address,
        hashlib.sha256(formula.encode()).hexdigest(),
        hashlib.sha256(source.encode()).hexdigest(),
    )


_MECHANICAL_NAMING_SYSTEM_PROMPT = (
    "You add the semantic layer (docstring and local names) to a verified, "
    "mechanically generated Python function. Return only JSON matching the schema."
)


def _semantic_naming_user_prompt(
    pending: _PendingSemanticUnit,
    *,
    internals_path: Path,
    internals_index: InternalsSourceIndex,
) -> str:
    from src.mechanical_naming import (
        format_cluster_naming_prompt_context,
        format_singleton_naming_prompt_context,
        load_cluster_naming_prompt_fixed_portion,
        load_singleton_naming_prompt_fixed_portion,
    )

    if pending.kind == "cluster":
        assert isinstance(pending.ctx, ClusterRefactorContext)
        context_dump = build_cluster_refactor_prompt_context(
            pending.ctx,
            internals_path=internals_path,
            internals_index=internals_index,
        )
        return (
            load_cluster_naming_prompt_fixed_portion().strip()
            + "\n\n"
            + format_cluster_naming_prompt_context(context_dump, pending.draft)
        )
    assert isinstance(pending.ctx, SingletonRefactorContext)
    context_dump = build_singleton_refactor_prompt_context(
        pending.ctx,
        internals_path=internals_path,
        internals_index=internals_index,
    )
    return (
        load_singleton_naming_prompt_fixed_portion().strip()
        + "\n\n"
        + format_singleton_naming_prompt_context(context_dump, pending.draft)
    )


def _apply_and_validate_semantic_naming(
    pending_units: Sequence[_PendingSemanticUnit],
    naming_by_unit: Mapping[str, ClusterNamingLLMResponse],
    source: str,
    *,
    runtime_source: str,
) -> tuple[
    str,
    dict[str, ClusterRefactorResponse | SingletonRefactorResponse],
    list[tuple[str, str, BaseException]],
]:
    """Validate naming responses, then apply them commutatively to ``source``.

    Each unit is prepared and fully validated (including semantic local names)
    before any rewrite. Units that fail prepare/validate are skipped (left
    mechanical) and listed in the returned skip triples
    ``(unit_id, helper_name, exc)``. Application goes through
    :func:`src.mechanical_naming.apply_naming_responses_to_module` so the live
    pass-2 path matches the order-independent applier covered by tests.
    Prepared responses have ``helper_source`` / ``symbol_source`` synced to the
    text actually written into the module.
    """
    from src.mechanical_naming import (
        NamingUnit,
        apply_cluster_naming_response,
        apply_naming_responses_to_module,
    )

    existing_names = _function_names(source)
    prepared_by_unit: dict[
        str, ClusterRefactorResponse | SingletonRefactorResponse
    ] = {}
    units: list[NamingUnit] = []
    skipped: list[tuple[str, str, BaseException]] = []

    for pending in pending_units:
        naming_response = naming_by_unit[pending.unit_id]
        try:
            named_body = apply_cluster_naming_response(
                naming_response,
                pending.draft,
                parameter_names=pending.parameter_names,
                forbidden_names=pending.forbidden_names,
            )
            if pending.kind == "cluster":
                assert isinstance(pending.ctx, ClusterRefactorContext)
                legacy = ClusterRefactorLLMResponse(
                    symbol_docstring=naming_response.symbol_docstring,
                    symbol_body=named_body,
                    error=None,
                    error_reason=None,
                )
                prepared: ClusterRefactorResponse | SingletonRefactorResponse = (
                    prepare_cluster_refactor_response(
                        legacy,
                        pending.ctx,
                        runtime_source=runtime_source,
                        internals_source=source,
                    )
                )
                assert isinstance(prepared, ClusterRefactorResponse)
                prepared = _prepare_cluster_refactor_response(prepared, pending.ctx)
                validate_cluster_refactor_response(
                    pending.ctx,
                    prepared,
                    existing_names=existing_names,
                    internals_source=source,
                    require_semantic_locals=True,
                )
                enriched_docstring = prepared.helper_docstring
            else:
                assert isinstance(pending.ctx, SingletonRefactorContext)
                legacy_singleton = SingletonRefactorLLMResponse(
                    symbol_docstring=naming_response.symbol_docstring,
                    symbol_body=named_body,
                    error=None,
                    error_reason=None,
                )
                prepared = prepare_singleton_refactor_response(
                    legacy_singleton,
                    pending.ctx,
                    runtime_source=runtime_source,
                    internals_source=source,
                )
                assert isinstance(prepared, SingletonRefactorResponse)
                prepared = _prepare_singleton_refactor_response(prepared, pending.ctx)
                validate_singleton_refactor_response(
                    pending.ctx,
                    prepared,
                    existing_names=existing_names,
                    internals_source=source,
                    require_semantic_locals=True,
                )
                enriched_docstring = prepared.symbol_docstring
        except (ValueError, ValidationError, TypeError, RefactorDeclaredError) as exc:
            logger.warning(
                "pass2 semantic naming apply skipped: helper=%s unit=%s error=%s",
                pending.helper_name,
                pending.unit_id,
                exc,
            )
            skipped.append((pending.unit_id, pending.helper_name, exc))
            continue

        prepared_by_unit[pending.unit_id] = prepared
        units.append(
            NamingUnit(
                helper_name=pending.helper_name,
                draft=pending.draft,
                response=naming_response.model_copy(
                    update={"symbol_docstring": enriched_docstring}
                ),
                parameter_names=pending.parameter_names,
                forbidden_names=pending.forbidden_names,
            )
        )

    if not units:
        return source, prepared_by_unit, skipped

    named_source = apply_naming_responses_to_module(source, units)
    for pending in pending_units:
        prepared_unit = prepared_by_unit.get(pending.unit_id)
        if prepared_unit is None:
            continue
        helper_source = extract_function_source(
            named_source, pending.helper_name
        ).strip()
        if pending.kind == "cluster":
            assert isinstance(prepared_unit, ClusterRefactorResponse)
            prepared_by_unit[pending.unit_id] = _align_cluster_response_docstring(
                prepared_unit.model_copy(update={"helper_source": helper_source})
            )
        else:
            assert isinstance(prepared_unit, SingletonRefactorResponse)
            prepared_by_unit[pending.unit_id] = _align_singleton_response_docstring(
                prepared_unit.model_copy(update={"symbol_source": helper_source})
            )
    return named_source, prepared_by_unit, skipped


def _refresh_mechanical_cluster_results(
    results: Sequence[ClusterRefactorApplyResult],
    *,
    pending_units: Sequence[_PendingSemanticUnit],
    prepared_by_unit_id: Mapping[
        str, ClusterRefactorResponse | SingletonRefactorResponse
    ],
    named_source: str,
) -> list[ClusterRefactorApplyResult]:
    """Replace pass-1 placeholder cluster responses with post-naming ones."""
    named_by_helper = {
        pending.helper_name: prepared_by_unit_id[pending.unit_id]
        for pending in pending_units
        if pending.kind == "cluster"
    }
    refreshed: list[ClusterRefactorApplyResult] = []
    for result in results:
        prepared = named_by_helper.get(result.helper_name)
        if prepared is None:
            refreshed.append(result)
            continue
        assert isinstance(prepared, ClusterRefactorResponse)
        refreshed.append(
            ClusterRefactorApplyResult(
                source=named_source,
                helper_name=result.helper_name,
                wrappers_applied=result.wrappers_applied,
                dry_run=result.dry_run,
                response=prepared,
                phase_c_pruned=result.phase_c_pruned,
            )
        )
    return refreshed


def _run_semantic_naming_pass(
    pending_units: Sequence[_PendingSemanticUnit],
    *,
    internals_path: Path,
    internals_index: InternalsSourceIndex,
    runtime_source: str,
    dry_run: bool,
) -> tuple[
    InternalsSourceIndex,
    dict[str, ClusterRefactorResponse | SingletonRefactorResponse],
]:
    """Pass 2: name every mechanical unit in parallel, then rewrite the module once.

    Naming is a pure semantic layer over an already-verified body, so the calls
    are independent and run concurrently under the shared LLM semaphore. Results
    are cached on a key that omits the internals hash (the mechanical body fully
    determines the answer). Each successful miss is written to the cache as it
    arrives. Units that fail naming (after retries) or apply/validate are left
    mechanical; a summary warning points at the standalone naming CLI for retry.
    Successful responses are applied to a single in-memory source that is written
    and re-indexed once.
    """
    from src.mechanical_naming import (
        ClusterNamingLLMResponse as ClusterNamingModel,
    )
    from src.mechanical_naming import (
        SingletonNamingLLMResponse,
        apply_cluster_naming_response,
    )

    model = refactor_model()
    cache = load_refactor_cache()

    cache_keys: dict[str, str] = {}
    prompts: dict[str, str] = {}
    response_models: dict[str, type[ClusterNamingLLMResponse]] = {}
    for pending in pending_units:
        response_model: type[ClusterNamingLLMResponse] = (
            ClusterNamingModel
            if pending.kind == "cluster"
            else SingletonNamingLLMResponse
        )
        response_models[pending.unit_id] = response_model
        cache_keys[pending.unit_id] = semantic_naming_cache_key(
            kind=pending.kind,
            unit_id=pending.unit_id,
            canonical_template=pending.canonical_template,
            mechanical_body=pending.draft.body,
            response_schema=response_model.model_json_schema(),
            member_fingerprints=pending.member_fingerprints,
            contract=pending.contract,
        )
        prompt = _semantic_naming_user_prompt(
            pending,
            internals_path=internals_path,
            internals_index=internals_index,
        )
        prompts[pending.unit_id] = prompt
        if _PROMPT_OBSERVER is not None:
            _PROMPT_OBSERVER(pending.kind, pending.helper_name, prompt)

    naming_by_unit: dict[str, ClusterNamingLLMResponse] = {}
    misses: list[_PendingSemanticUnit] = []
    for pending in pending_units:
        cached = cache.get(cache_keys[pending.unit_id])
        if cached is None:
            misses.append(pending)
            continue
        try:
            naming_response = response_models[pending.unit_id].model_validate_json(
                cached
            )
            apply_cluster_naming_response(
                naming_response,
                pending.draft,
                parameter_names=pending.parameter_names,
                forbidden_names=pending.forbidden_names,
            )
        except (ValueError, ValidationError):
            del cache[cache_keys[pending.unit_id]]
            misses.append(pending)
            continue
        naming_by_unit[pending.unit_id] = naming_response

    skipped: list[tuple[str, str, BaseException]] = []
    if misses:

        def _persist_success(
            unit_id: str, naming_response: ClusterNamingLLMResponse
        ) -> None:
            naming_by_unit[unit_id] = naming_response
            if not dry_run:
                cache[cache_keys[unit_id]] = naming_response.model_dump_json()
                save_refactor_cache(cache)

        _gathered, gather_skipped = _gather_semantic_naming(
            misses,
            model=model,
            prompts=prompts,
            on_success=_persist_success,
        )
        # Prefer gather's return over on_success alone so patched gathers that
        # skip the callback still wire successes into apply.
        naming_by_unit.update(_gathered)
        skipped.extend(gather_skipped)

    apply_units = [
        pending for pending in pending_units if pending.unit_id in naming_by_unit
    ]
    if apply_units:
        source, prepared_by_unit, apply_skipped = _apply_and_validate_semantic_naming(
            apply_units,
            naming_by_unit,
            internals_index.source,
            runtime_source=runtime_source,
        )
        for unit_id, helper_name, exc in apply_skipped:
            skipped.append((unit_id, helper_name, exc))
            cache_key = cache_keys.get(unit_id)
            if cache_key is not None and cache_key in cache:
                del cache[cache_key]
                naming_by_unit.pop(unit_id, None)
    else:
        source = internals_index.source
        prepared_by_unit = {}

    if skipped:
        helpers = ", ".join(sorted({helper for _, helper, _ in skipped}))
        logger.warning(
            "pass2 semantic naming skipped %d helper(s) (%s); left mechanical. "
            "Retry with: uv run python -m scripts.run_semantic_naming "
            "--internals %s",
            len(skipped),
            helpers,
            internals_path,
        )

    if apply_units:
        validate_refactored_internals(source)
        if not dry_run:
            internals_path.write_text(source, encoding="utf-8", newline="\n")
            save_refactor_cache(cache)
    elif skipped and not dry_run:
        # Evict any sticky bad cache entries even when nothing was rewritten.
        save_refactor_cache(cache)

    return InternalsSourceIndex.from_source(source), prepared_by_unit


def _gather_semantic_naming(
    misses: Sequence[_PendingSemanticUnit],
    *,
    model: str,
    prompts: Mapping[str, str],
    on_success: Callable[[str, ClusterNamingLLMResponse], None] | None = None,
) -> tuple[
    dict[str, ClusterNamingLLMResponse],
    list[tuple[str, str, BaseException]],
]:
    from src.mechanical_naming import (
        ClusterNamingLLMResponse as ClusterNamingModel,
    )
    from src.mechanical_naming import (
        SingletonNamingLLMResponse,
        apply_cluster_naming_response,
    )

    client, provider = build_async_client(model)
    semaphore = get_llm_semaphore()
    failures: list[tuple[str, str, BaseException]] = []

    def _make_post_validate(pending: _PendingSemanticUnit):
        def _post_validate(
            parsed: ClusterNamingLLMResponse,
        ) -> ClusterNamingLLMResponse:
            raise_if_llm_declared_error(
                parsed,
                kind=pending.kind,
                target=pending.diagnostic_target,
            )
            apply_cluster_naming_response(
                parsed,
                pending.draft,
                parameter_names=pending.parameter_names,
                forbidden_names=pending.forbidden_names,
            )
            return parsed

        return _post_validate

    async def _one(
        pending: _PendingSemanticUnit,
    ) -> tuple[str, ClusterNamingLLMResponse]:
        response_model: type[ClusterNamingLLMResponse] = (
            ClusterNamingModel
            if pending.kind == "cluster"
            else SingletonNamingLLMResponse
        )
        parsed, _content = await generate_validated_json_async(
            client=client,
            model=model,
            provider=provider,
            system_prompt=_MECHANICAL_NAMING_SYSTEM_PROMPT,
            user_prompt=prompts[pending.unit_id],
            response_model=response_model,
            post_validate=_make_post_validate(pending),
            max_attempts=DEFAULT_MAX_ATTEMPTS,
            semaphore=semaphore,
        )
        return pending.unit_id, parsed

    def _on_error(pending: _PendingSemanticUnit, exc: BaseException) -> None:
        logger.warning(
            "pass2 semantic naming gather skipped: helper=%s unit=%s error=%s",
            pending.helper_name,
            pending.unit_id,
            exc,
        )
        failures.append((pending.unit_id, pending.helper_name, exc))

    successes = run_map_as_completed(
        misses,
        _one,
        on_success=on_success,
        on_error=_on_error,
        raise_on_error=False,
    )
    return successes, failures


def refactor_internals_all_clusters(
    projection: ProjectionResult,
    clusters: tuple[FormulaCluster, ...],
    *,
    internals_path: Path,
    bindings_path: Path,
    workbook_path: Path,
    address_to_series_id: Mapping[str, str],
    dry_run: bool = False,
    source_graph: DependencyGraph | None = None,
    internal_binding_index: InternalBindingIndex | None = None,
    bound_address_keys: dict[str, dict[str, BindingKeyValue]] | None = None,
    key_vocabulary: tuple[KeyConceptSpec, ...] | None = None,
    parity_gate: bool = True,
    constraints: Mapping[str, object] | None = None,
    refactor_schedule: tuple[RefactorUnit, ...] | None = None,
    timer: StageTimer | None = None,
    codegen_cache_key: str | None = None,
) -> InternalsRefactorRunResult:
    """Refactor every eligible unit in unified dependency order in two passes.

    Pass 1 (sequential, no LLM): each unit whose body can be mechanically
    synthesized is collapsed with a placeholder docstring, applied in schedule
    order so later units see upstream collapses. Units that cannot be mechanically
    synthesized fall back to the interleaved full-body LLM contract (with its own
    per-unit parity gate) exactly as before.

    Full-module ``validate_refactored_internals`` (parse + compile) runs once after
    Pass 1 applies, not after every unit. Duplicate top-level names are still
    checked incrementally via a live function-name set during applies.

    Independent mechanical cluster collapses are accumulated and applied with
    :func:`apply_cluster_collapses_batch` so a topological layer shares one
    full-module ``ast.parse`` instead of paying parse cost per unit.

    Between the passes, when ``parity_gate`` is enabled, all mechanically
    refactored helpers are checked against the pristine cell semantics in a single
    batched gate. A mechanical divergence is a synthesis defect and raises loudly
    with no retry.

    Pass 2 (parallel): the deferred semantic layer — docstring and local names —
    is generated for every mechanical unit concurrently, applied to the module in
    one shot, and fully re-validated (including semantic local names).

    Disk flush boundary: Pass 1 never writes ``internals_path`` per unit.
    Cumulative source is threaded through the in-memory ``current_source``. After
    Pass 1 structural validate succeeds, the mechanical module is checkpointed
    under ``.cache/internals/<package-namespace>/internals.mechanical.py`` before
    the batched parity gate runs so a mid-gate kill does not lose the apply work.
    The package ``internals_path`` is promoted only after the gate passes (or when
    the gate is skipped), still before Pass 2 — unless ``dry_run``. A parity
    failure leaves the package path pristine and retains the checkpoint. On
    successful promotion the checkpoint is deleted. Pass 2 and Phase C keep their
    own single writes.

    Pass ``bound_address_keys`` from the extract stage when available so cluster
    context construction does not fall back to the series-derived cache lookup.
    Pass ``key_vocabulary`` when available so context construction does not call
    ``_default_key_vocabulary`` (which reloads and YAML-parses
    ``bindings/*.bindings.yaml`` once per unit).

    Pass ``timer`` to collect the Pass 1 / parity-gate / Pass 2 wall clock as
    spans of the caller's refactor stage; every span is also logged, so callers
    without a timer keep the same diagnostics.
    """
    pristine_source: str | None = None
    input_vectors: Sequence[Mapping[str, object]] | None = None
    internals_index = _resolve_internals_index(internals_path)

    # Patch package runtime early so mechanical @xl_memoize imports resolve and
    # the allowlist / parity gate see xl_helper before Pass 1 applies units.
    from src.helper_memoization import ensure_package_runtime_helper_memoization
    from src.refactor_parity_gate import clear_parity_runtime_caches

    package_root = internals_path.parent
    runtime_file = package_root / "runtime.py"
    if runtime_file.is_file() and ensure_package_runtime_helper_memoization(
        runtime_file
    ):
        clear_parity_runtime_caches()
        clear_runtime_symbol_caches()
        logger.info(
            "pass1 patched package runtime with helper memoization: path=%s",
            runtime_file,
        )

    if parity_gate:
        from src.refactor_parity_gate import build_default_input_vectors

        if constraints is None:
            raise ValueError("constraints is required when parity_gate is enabled")
        if codegen_cache_key is not None:
            from src.package_materialize import load_pristine_internals_from_codegen

            pristine_source = load_pristine_internals_from_codegen(codegen_cache_key)
        else:
            # Lab/unit-test path when no codegen key is threaded through.
            pristine_source = internals_index.source
        input_vectors = build_default_input_vectors(
            package_root=package_root,
            constraints=constraints,
        )

    runtime_source = _read_runtime_source(internals_path)
    callee_hints = build_callee_return_hints(
        runtime_source=runtime_source,
        internals_source=internals_index.source,
    )
    ordered_units = (
        refactor_schedule
        if refactor_schedule is not None
        else compute_refactor_schedule(projection, clusters)
    )
    existing_helper_names = internals_index.semantic_helper_names
    allocated_helper_names = allocate_schedule_helper_names(
        tuple(unit.members for unit in ordered_units),
        address_to_series_id,
        existing_names=existing_helper_names,
    )
    allocated_name_set = frozenset(allocated_helper_names)
    # Helper names are locked for every unit up front, so each scheduled address
    # has an exact owning helper even when its series is peeled across units
    # (``series_id`` / ``series_id_2``). Dependency resolution needs that owner to
    # route reads which cross a peel boundary (issue #138).
    scheduled_helper_by_address = {
        address: name
        for unit, name in zip(ordered_units, allocated_helper_names, strict=True)
        for address in unit.members
    }
    results: list[ClusterRefactorApplyResult] = []
    pending_semantic: list[_PendingSemanticUnit] = []
    pending_cluster_applies: list[_PendingMechanicalClusterApply] = []
    pending_batch_members: set[str] = set()
    refactored_any = False
    ungated_full_body = False
    current_source = internals_index.source
    dirty_addresses: set[str] = set()
    live_function_names = set(internals_index.functions)
    pass1_reindex_count = 0
    pass1_apply_count = 0
    pass1_batch_count = 0
    pass1_started = time.perf_counter()
    pass1_apply_seconds = 0.0
    pass1_validate_seconds = 0.0
    pass1_reindex_seconds = 0.0
    pass1_context_seconds = 0.0
    pass1_synthesize_seconds = 0.0
    timing_active = _pass1_unit_timing_active()

    def _record_span(
        name: str,
        seconds: float,
        *,
        attach: bool = True,
        **metrics: float | str,
    ) -> None:
        """Log one refactor span; attach leaf spans to the caller's stage timer.

        Rollups such as ``pass1`` stay log-only (``attach=False``) so flat
        ``stages[].spans`` does not double-count with ``pass1_*`` components.
        """
        metric_text = ", ".join(f"{key}={value}" for key, value in metrics.items())
        logger.info(
            "refactor span %s: %.1fs%s",
            name,
            seconds,
            f" ({metric_text})" if metric_text else "",
        )
        if attach and timer is not None:
            timer.record(name, seconds)

    def _unit_reads_addresses(members: Sequence[str], addresses: set[str]) -> bool:
        if not addresses:
            return False
        for member in members:
            for dependency in projection.get_dependencies(member):
                if dependency in addresses:
                    return True
                for transitive in projection.get_dependencies(dependency):
                    if transitive in addresses:
                        return True
        return False

    def _unit_reads_dirty(members: Sequence[str]) -> bool:
        # Context builders consume member bodies (rewritten when a direct
        # dependency collapses) and direct-dependency def nodes (rewritten when
        # that dependency or one of its own dependencies collapses), so probing
        # two dependency levels bounds every source a unit can read.
        return _unit_reads_addresses(members, dirty_addresses)

    def _seal_index() -> float:
        nonlocal internals_index, pass1_reindex_count, pass1_reindex_seconds
        if not dirty_addresses:
            return 0.0
        seal_started = time.perf_counter()
        internals_index = InternalsSourceIndex.from_source(current_source)
        # Refresh callee return hints from the resealed module so a later cluster
        # whose member body is a bare call to a helper created earlier in this
        # pass can infer its return type (reuses the just-parsed defs; no extra
        # full-module parse).
        merge_callee_return_hints_from_functions(
            callee_hints, internals_index.functions
        )
        seal_seconds = time.perf_counter() - seal_started
        pass1_reindex_count += 1
        pass1_reindex_seconds += seal_seconds
        dirty_addresses.clear()
        return seal_seconds

    def _emit_unit_timing(
        *,
        unit_id: str,
        kind: Literal["singleton", "cluster"],
        member_count: int,
        context_s: float,
        synthesize_s: float,
        apply_s: float,
        reindex_s: float,
        reindexed: bool,
        apply_batch_size: int,
        mechanical: bool,
    ) -> None:
        if not timing_active:
            return
        _emit_pass1_unit_timing(
            Pass1UnitTiming(
                unit_id=unit_id,
                kind=kind,
                member_count=member_count,
                context_s=context_s,
                synthesize_s=synthesize_s,
                apply_s=apply_s,
                # Full-module validate is deferred to end of Pass 1.
                validate_s=0.0,
                reindex_s=reindex_s,
                reindexed=reindexed,
                apply_batch_size=apply_batch_size,
                dirty_count=len(dirty_addresses),
                source_bytes=len(current_source.encode("utf-8")),
                mechanical=mechanical,
            )
        )

    def _flush_mechanical_cluster_batch() -> None:
        nonlocal current_source, pass1_apply_count, pass1_apply_seconds
        nonlocal pass1_batch_count, refactored_any
        if not pending_cluster_applies:
            return
        responses = tuple(item.mechanical_response for item in pending_cluster_applies)
        batch_size = len(pending_cluster_applies)
        apply_started = time.perf_counter()
        try:
            updated, _rewrite_count = apply_cluster_collapses_batch(
                current_source, responses
            )
        except Exception as error:
            first = pending_cluster_applies[0]
            _log_mechanical_pass1_failure(
                kind="cluster",
                target=first.diagnostic_target,
                error=error,
                prepared_response=_response_dump(first.mechanical_response),
                context=lambda: {
                    **_mechanical_cluster_failure_context(
                        first.cluster_ctx, first.draft
                    ),
                    "batch_size": len(pending_cluster_applies),
                    "batch_targets": [
                        item.diagnostic_target for item in pending_cluster_applies
                    ],
                },
                log_message=(
                    "cluster mechanical batch apply failed first_cluster_id=%s "
                    "batch_size=%s diagnostic=%s: %s"
                ),
                log_args=(
                    first.cluster_ctx.cluster_id,
                    len(pending_cluster_applies),
                ),
            )
            raise
        batch_apply_seconds = time.perf_counter() - apply_started
        pass1_apply_seconds += batch_apply_seconds
        pass1_batch_count += 1
        current_source = updated
        per_unit_apply_s = batch_apply_seconds / batch_size
        for item in pending_cluster_applies:
            merge_callee_return_hints(
                callee_hints,
                source=item.mechanical_response.helper_source,
            )
        for item in pending_cluster_applies:
            dirty_addresses.update(item.cluster_members)
            pass1_apply_count += 1
            results.append(
                ClusterRefactorApplyResult(
                    source=updated,
                    helper_name=item.mechanical_response.helper_name,
                    wrappers_applied=tuple(
                        entry.function_name
                        for entry in item.mechanical_response.member_keys
                    ),
                    dry_run=dry_run,
                    response=item.mechanical_response,
                )
            )
            pending_semantic.append(
                _PendingSemanticUnit(
                    kind="cluster",
                    unit_id=item.diagnostic_target,
                    helper_name=item.mechanical_response.helper_name,
                    diagnostic_target=item.diagnostic_target,
                    canonical_template=item.cluster_ctx.canonical_template,
                    contract=item.cluster_ctx.contract,
                    draft=item.draft,
                    ctx=item.cluster_ctx,
                    parameter_names=frozenset(
                        parameter.name
                        for parameter in item.mechanical_response.parameters
                    ),
                    forbidden_names=item.existing_names
                    | allocated_name_set
                    | frozenset(item.cluster_ctx.allowed_runtime_symbols),
                    member_fingerprints=tuple(
                        _member_fingerprint(
                            member.address,
                            member.normalized_formula,
                            member.python_source,
                        )
                        for member in item.cluster_ctx.members
                    ),
                    member_checks=tuple(
                        (
                            entry.address,
                            _parameter_literals(
                                item.mechanical_response.parameters,
                                entry.keys_dict(),
                            ),
                        )
                        for entry in item.mechanical_response.member_keys
                    ),
                )
            )
            refactored_any = True
            _emit_unit_timing(
                unit_id=item.diagnostic_target,
                kind="cluster",
                member_count=len(item.cluster_members),
                context_s=item.timing_context_s,
                synthesize_s=item.timing_synthesize_s,
                apply_s=per_unit_apply_s,
                reindex_s=item.timing_reindex_s,
                reindexed=item.timing_reindexed,
                apply_batch_size=batch_size,
                mechanical=True,
            )
        pending_cluster_applies.clear()
        pending_batch_members.clear()

    def _prepare_for_unit(members: Sequence[str]) -> tuple[float, bool]:
        if pending_batch_members and _unit_reads_addresses(
            members, pending_batch_members
        ):
            _flush_mechanical_cluster_batch()
        if dirty_addresses and _unit_reads_dirty(members):
            _flush_mechanical_cluster_batch()
            return _seal_index(), True
        return 0.0, False

    for unit, helper_name in zip(ordered_units, allocated_helper_names, strict=True):
        cluster = unit.as_formula_cluster()
        diagnostic_target = refactor_failure_target(unit)
        unit_reindex_s, unit_reindexed = _prepare_for_unit(cluster.members)
        reserved_for_others = (
            frozenset(allocated_helper_names) | existing_helper_names
        ) - {helper_name}
        if len(cluster.members) == 1:
            _flush_mechanical_cluster_batch()
            context_started = time.perf_counter()
            singleton_ctx = build_singleton_refactor_context(
                projection,
                cluster,
                internals_path,
                source_graph=source_graph,
                internal_binding_index=internal_binding_index,
                internals_index=internals_index,
                address_to_series_id=address_to_series_id,
                address_to_helper_name=scheduled_helper_by_address,
                bound_address_keys=bound_address_keys,
                expected_helper_name=helper_name,
                existing_helper_names=reserved_for_others,
            )
            context_s = time.perf_counter() - context_started
            pass1_context_seconds += context_s
            if singleton_ctx is None:
                continue
            if _SINGLETON_CONTEXT_OBSERVER is not None:
                _SINGLETON_CONTEXT_OBSERVER(singleton_ctx)
            synthesize_started = time.perf_counter()
            draft = _try_synthesize_singleton_body(singleton_ctx)
            synthesize_s = time.perf_counter() - synthesize_started
            pass1_synthesize_seconds += synthesize_s
            if draft is not None:
                internals_source = internals_index.source
                existing_names = _function_names(
                    internals_source, index=internals_index
                )
                prepared_dump: Mapping[str, Any] | None = None
                try:
                    mechanical_response = build_mechanical_singleton_response(
                        singleton_ctx,
                        draft,
                        runtime_source=runtime_source,
                        internals_source=internals_source,
                        callee_hints=callee_hints,
                    )
                    prepared_dump = _response_dump(mechanical_response)
                    validate_singleton_refactor_response(
                        singleton_ctx,
                        mechanical_response,
                        existing_names=existing_names,
                        internals_source=internals_source,
                        require_semantic_locals=False,
                    )
                    _record_singleton_name_delta(
                        live_function_names,
                        old_name=singleton_ctx.function_name,
                        new_name=mechanical_response.symbol_name,
                    )
                    apply_started = time.perf_counter()
                    updated, _rewrites = apply_singleton_refactor_plan(
                        current_source, mechanical_response, singleton_ctx
                    )
                    apply_s = time.perf_counter() - apply_started
                    pass1_apply_seconds += apply_s
                except Exception as error:
                    _log_mechanical_pass1_failure(
                        kind="singleton",
                        target=diagnostic_target,
                        error=error,
                        prepared_response=prepared_dump,
                        context=lambda ctx=singleton_ctx, d=draft: (
                            _mechanical_singleton_failure_context(ctx, d)
                        ),
                        log_message=(
                            "singleton mechanical refactor failed address=%s "
                            "diagnostic=%s: %s"
                        ),
                        log_args=(singleton_ctx.address,),
                    )
                    raise
                current_source = updated
                merge_callee_return_hints(
                    callee_hints,
                    source=mechanical_response.symbol_source,
                )
                dirty_addresses.update(cluster.members)
                pass1_apply_count += 1
                pending_semantic.append(
                    _PendingSemanticUnit(
                        kind="singleton",
                        unit_id=diagnostic_target,
                        helper_name=mechanical_response.symbol_name,
                        diagnostic_target=diagnostic_target,
                        canonical_template=singleton_ctx.canonical_template,
                        contract=None,
                        draft=draft,
                        ctx=singleton_ctx,
                        parameter_names=frozenset(),
                        forbidden_names=existing_names
                        | allocated_name_set
                        | frozenset(singleton_ctx.allowed_runtime_symbols),
                        member_fingerprints=(
                            _member_fingerprint(
                                singleton_ctx.address,
                                singleton_ctx.normalized_formula,
                                singleton_ctx.python_source,
                            ),
                        ),
                        member_checks=((singleton_ctx.address, {}),),
                    )
                )
                refactored_any = True
                _emit_unit_timing(
                    unit_id=diagnostic_target,
                    kind="singleton",
                    member_count=1,
                    context_s=context_s,
                    synthesize_s=synthesize_s,
                    apply_s=apply_s,
                    reindex_s=unit_reindex_s,
                    reindexed=unit_reindexed,
                    apply_batch_size=1,
                    mechanical=True,
                )
                continue
            if not parity_gate:
                ungated_full_body = True
            apply_started = time.perf_counter()
            singleton_result = refactor_internals_singleton(
                singleton_ctx,
                internals_path=internals_path,
                dry_run=dry_run,
                pristine_source=pristine_source,
                input_vectors=input_vectors,
                source_graph=source_graph,
                diagnostic_target=diagnostic_target,
                internals_index=internals_index,
                flush=False,
                apply_source=current_source,
                validate_module=False,
            )
            apply_s = time.perf_counter() - apply_started
            if not dry_run:
                _record_singleton_name_delta(
                    live_function_names,
                    old_name=singleton_ctx.function_name,
                    new_name=singleton_result.symbol_name,
                )
                current_source = singleton_result.source
                dirty_addresses.update(cluster.members)
                pass1_apply_count += 1
                pass1_apply_seconds += apply_s
            refactored_any = True
            _emit_unit_timing(
                unit_id=diagnostic_target,
                kind="singleton",
                member_count=1,
                context_s=context_s,
                synthesize_s=synthesize_s,
                apply_s=apply_s,
                reindex_s=unit_reindex_s,
                reindexed=unit_reindexed,
                apply_batch_size=1,
                mechanical=False,
            )
            continue

        context_started = time.perf_counter()
        cluster_ctx = build_cluster_refactor_context(
            projection,
            cluster,
            internals_path,
            source_graph=source_graph,
            internal_binding_index=internal_binding_index,
            bound_address_keys=bound_address_keys,
            key_vocabulary=key_vocabulary,
            bindings_path=bindings_path,
            workbook_path=workbook_path,
            internals_index=internals_index,
            address_to_series_id=address_to_series_id,
            address_to_helper_name=scheduled_helper_by_address,
            expected_helper_name=helper_name,
            existing_helper_names=reserved_for_others,
        )
        context_s = time.perf_counter() - context_started
        pass1_context_seconds += context_s
        if cluster_ctx is None:
            continue
        if _CLUSTER_CONTEXT_OBSERVER is not None:
            _CLUSTER_CONTEXT_OBSERVER(cluster_ctx)
        synthesize_started = time.perf_counter()
        draft = _try_synthesize_cluster_body(cluster_ctx)
        synthesize_s = time.perf_counter() - synthesize_started
        pass1_synthesize_seconds += synthesize_s
        if draft is not None:
            internals_source = internals_index.source
            existing_names = _function_names(internals_source, index=internals_index)
            prepared_dump = None
            try:
                mechanical_response = build_mechanical_cluster_response(
                    cluster_ctx,
                    draft,
                    runtime_source=runtime_source,
                    internals_source=internals_source,
                    callee_hints=callee_hints,
                )
                prepared_dump = _response_dump(mechanical_response)
                validate_cluster_refactor_response(
                    cluster_ctx,
                    mechanical_response,
                    existing_names=existing_names,
                    internals_source=internals_source,
                    require_semantic_locals=False,
                )
                removed_names = tuple(
                    entry.function_name for entry in mechanical_response.member_keys
                )
                _record_cluster_name_delta(
                    live_function_names,
                    helper_name=mechanical_response.helper_name,
                    removed_names=removed_names,
                )
            except Exception as error:
                _log_mechanical_pass1_failure(
                    kind="cluster",
                    target=diagnostic_target,
                    error=error,
                    prepared_response=prepared_dump,
                    context=lambda ctx=cluster_ctx, d=draft: (
                        _mechanical_cluster_failure_context(ctx, d)
                    ),
                    log_message=(
                        "cluster mechanical refactor failed cluster_id=%s "
                        "diagnostic=%s: %s"
                    ),
                    log_args=(cluster_ctx.cluster_id,),
                )
                raise
            pending_cluster_applies.append(
                _PendingMechanicalClusterApply(
                    cluster_members=tuple(cluster.members),
                    cluster_ctx=cluster_ctx,
                    draft=draft,
                    mechanical_response=mechanical_response,
                    existing_names=existing_names,
                    diagnostic_target=diagnostic_target,
                    timing_context_s=context_s,
                    timing_synthesize_s=synthesize_s,
                    timing_reindex_s=unit_reindex_s,
                    timing_reindexed=unit_reindexed,
                )
            )
            pending_batch_members.update(cluster.members)
            continue
        _flush_mechanical_cluster_batch()
        if dirty_addresses and _unit_reads_dirty(cluster.members):
            unit_reindex_s += _seal_index()
            unit_reindexed = True
        if not parity_gate:
            ungated_full_body = True
        apply_started = time.perf_counter()
        result = refactor_internals_cluster(
            cluster_ctx,
            internals_path=internals_path,
            dry_run=dry_run,
            pristine_source=pristine_source,
            input_vectors=input_vectors,
            source_graph=source_graph,
            diagnostic_target=diagnostic_target,
            internals_index=internals_index,
            flush=False,
            apply_source=current_source,
            validate_module=False,
        )
        apply_s = time.perf_counter() - apply_started
        if not dry_run:
            _record_cluster_name_delta(
                live_function_names,
                helper_name=result.helper_name,
                removed_names=result.wrappers_applied,
            )
            current_source = result.source
            dirty_addresses.update(cluster.members)
            pass1_apply_count += 1
            pass1_apply_seconds += apply_s
        results.append(result)
        refactored_any = True
        _emit_unit_timing(
            unit_id=diagnostic_target,
            kind="cluster",
            member_count=len(cluster.members),
            context_s=context_s,
            synthesize_s=synthesize_s,
            apply_s=apply_s,
            reindex_s=unit_reindex_s,
            reindexed=unit_reindexed,
            apply_batch_size=1,
            mechanical=False,
        )

    _flush_mechanical_cluster_batch()
    _seal_index()
    if pass1_apply_count:
        validate_started = time.perf_counter()
        validate_refactored_internals(current_source)
        pass1_validate_seconds = time.perf_counter() - validate_started
    pass1_elapsed = time.perf_counter() - pass1_started
    logger.info(
        "pass1 index cadence: %d InternalsSourceIndex rebuild(s) for %d applied "
        "unit(s) across %d scheduled unit(s)",
        pass1_reindex_count,
        pass1_apply_count,
        len(ordered_units),
    )
    _record_span("pass1_context", pass1_context_seconds, units=len(ordered_units))
    _record_span("pass1_synthesize", pass1_synthesize_seconds)
    _record_span(
        "pass1_apply",
        pass1_apply_seconds,
        apply_batches=pass1_batch_count,
        applied_units=pass1_apply_count,
    )
    _record_span("pass1_validate", pass1_validate_seconds)
    _record_span("pass1_reindex", pass1_reindex_seconds, count=pass1_reindex_count)
    _record_span("pass1", pass1_elapsed, attach=False)

    # Use the validated live source for checkpoint / parity / promote. After the
    # end-of-pass seal this matches ``internals_index.source``; binding all three
    # to one name keeps them from drifting if that invariant is ever weakened.
    mechanical_source = current_source
    checkpoint_path = mechanical_internals_checkpoint_path(internals_path)
    if not dry_run and refactored_any:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(mechanical_source, encoding="utf-8", newline="\n")
        logger.info(
            "pass1 mechanical checkpoint: path=%s bytes=%d applied_units=%d",
            checkpoint_path,
            checkpoint_path.stat().st_size,
            pass1_apply_count,
        )

    parity_started = time.perf_counter()
    if parity_gate and pending_semantic and pristine_source is not None:
        from src.refactor_parity_gate import (
            MechanicalParityUnit,
            check_batched_mechanical_parity,
        )

        check_batched_mechanical_parity(
            pristine_source=pristine_source,
            mechanical_source=mechanical_source,
            package_root=package_root,
            units=[
                MechanicalParityUnit(
                    unit_id=pending.unit_id,
                    helper_name=pending.helper_name,
                    kind=pending.kind,
                    member_checks=pending.member_checks,
                )
                for pending in pending_semantic
            ],
            input_vectors=input_vectors if input_vectors is not None else (),
        )
    _record_span(
        "mechanical_parity_gate",
        time.perf_counter() - parity_started,
        units=len(pending_semantic) if parity_gate else 0,
    )

    if not dry_run and refactored_any:
        internals_path.write_text(mechanical_source, encoding="utf-8", newline="\n")
        checkpoint_path.unlink(missing_ok=True)

    pass2_started = time.perf_counter()
    if pending_semantic:
        internals_index, prepared_by_unit = _run_semantic_naming_pass(
            pending_semantic,
            internals_path=internals_path,
            internals_index=internals_index,
            runtime_source=runtime_source,
            dry_run=dry_run,
        )
        results = _refresh_mechanical_cluster_results(
            results,
            pending_units=pending_semantic,
            prepared_by_unit_id=prepared_by_unit,
            named_source=internals_index.source,
        )
    _record_span(
        "pass2_semantic_naming",
        time.perf_counter() - pass2_started,
        units=len(pending_semantic),
    )

    phase_c_started = time.perf_counter()
    if not dry_run and refactored_any:
        source = internals_path.read_text(encoding="utf-8")
        updated, phase_c_pruned = apply_phase_c(source)
        updated = rehome_unrefactored_cell_functions(updated)
        updated = _unify_peel_split_entrypoints(
            updated,
            scheduled_helper_by_address=scheduled_helper_by_address,
            address_to_series_id=address_to_series_id,
            bound_address_keys=bound_address_keys,
        )
        validate_refactored_internals(updated)
        internals_path.write_text(updated, encoding="utf-8", newline="\n")
        current_source = updated
        if results:
            last = results[-1]
            results[-1] = ClusterRefactorApplyResult(
                source=updated,
                helper_name=last.helper_name,
                wrappers_applied=last.wrappers_applied,
                dry_run=last.dry_run,
                response=last.response,
                phase_c_pruned=phase_c_pruned,
            )
    _record_span("phase_c", time.perf_counter() - phase_c_started)

    if not dry_run and internals_path.is_file():
        final_source = internals_path.read_text(encoding="utf-8")
    else:
        final_source = current_source
    cacheable = (not dry_run) and parity_gate and (not ungated_full_body)
    return InternalsRefactorRunResult(
        apply_results=tuple(results),
        final_source=final_source,
        cacheable=cacheable,
    )


load_dotenv(repo_root / ".env")


def _read_runtime_source(
    internals_path: Path,
    *,
    runtime_path: Path | None = None,
) -> str:
    """Load runtime (and optional ``_readers``) for dependency stubs and return hints."""
    parts: list[str] = []
    resolved_runtime = (
        runtime_path
        if runtime_path is not None
        else internals_path.parent / "runtime.py"
    )
    if resolved_runtime.is_file():
        parts.append(resolved_runtime.read_text(encoding="utf-8"))
    readers_path = internals_path.parent / "_readers.py"
    if readers_path.is_file():
        parts.append(readers_path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def llm_refactor_singleton(
    ctx: SingletonRefactorContext,
    *,
    internals_path: Path,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> SingletonRefactorResponse:
    mechanical_draft = _try_synthesize_singleton_body(ctx)
    if mechanical_draft is not None:
        from src.mechanical_naming import SingletonNamingLLMResponse

        llm_schema: dict[str, object] = {
            "schema": SingletonNamingLLMResponse.model_json_schema(),
            "mechanical_body": mechanical_draft.body,
        }
    else:
        llm_schema = SingletonRefactorLLMResponse.model_json_schema()
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    internals_bytes = index.source.encode("utf-8")
    internals_source = index.source
    runtime_source = _read_runtime_source(internals_path)
    existing_names = _function_names(internals_source, index=index)
    cache = load_refactor_cache()
    cache_key = singleton_refactor_cache_key(ctx, internals_bytes, llm_schema)
    failure_target = diagnostic_target or ctx.address

    def _build_user_prompt() -> str:
        context_dump = build_singleton_refactor_prompt_context(
            ctx,
            internals_path=internals_path,
            internals_index=index,
        )
        if mechanical_draft is not None:
            from src.mechanical_naming import (
                format_singleton_naming_prompt_context,
                load_singleton_naming_prompt_fixed_portion,
            )

            return (
                load_singleton_naming_prompt_fixed_portion().strip()
                + "\n\n"
                + format_singleton_naming_prompt_context(context_dump, mechanical_draft)
            )
        return _prompt_for_singleton_refactor(context_dump)

    if _SINGLETON_CONTEXT_OBSERVER is not None:
        _SINGLETON_CONTEXT_OBSERVER(ctx)
    if _PROMPT_OBSERVER is not None:
        _PROMPT_OBSERVER("singleton", failure_target, _build_user_prompt())

    def _apply_singleton_refactor_validation(
        prepared: SingletonRefactorResponse,
    ) -> SingletonRefactorResponse:
        prepared = _prepare_singleton_refactor_response(prepared, ctx)
        validate_singleton_refactor_response(
            ctx,
            prepared,
            existing_names=existing_names,
            internals_source=internals_source,
        )
        if pristine_source is not None and input_vectors is not None:
            from src.refactor_parity_gate import check_singleton_parity

            check_singleton_parity(
                pristine_source=pristine_source,
                current_source=internals_source,
                response=prepared,
                ctx=ctx,
                input_vectors=input_vectors,
                package_root=internals_path.parent,
            )
        return prepared

    def _finalize_singleton_from_llm(
        parsed: SingletonRefactorLLMResponse,
    ) -> SingletonRefactorResponse:
        prepared = prepare_singleton_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        return _apply_singleton_refactor_validation(prepared)

    def _validate_cached_singleton_response(
        cached_response: SingletonRefactorResponse,
    ) -> SingletonRefactorResponse:
        try:
            return _apply_singleton_refactor_validation(cached_response)
        except Exception as error:
            dump_dir = write_refactor_failure_diagnostic(
                kind="singleton",
                target=failure_target,
                error=error,
                llm_response=cached_response.model_dump(),
                prepared_response=cached_response.model_dump(),
                source="cache",
            )
            logger.error(
                "singleton refactor failed address=%s source=cache diagnostic=%s: %s",
                ctx.address,
                dump_dir,
                error,
            )
            raise

    cached_content = cache.get(cache_key)
    if cached_content is not None:
        try:
            logger.info(
                "singleton refactor cache hit address=%s key=%s",
                ctx.address,
                cache_key[:12],
            )
            return _validate_cached_singleton_response(
                SingletonRefactorResponse.model_validate_json(cached_content)
            )
        except (ValueError, ValidationError) as error:
            if not _refactor_provider_key_present():
                raise
            logger.warning(
                "singleton refactor cache stale address=%s key=%s: %s",
                ctx.address,
                cache_key[:12],
                error,
            )
            del cache[cache_key]
            save_refactor_cache(cache)

    model = refactor_model()
    client, provider = build_client(model)
    user_prompt = _build_user_prompt()
    attempt_artifacts: list[dict[str, Any]] = []
    validated_prepared: SingletonRefactorResponse | None = None

    def _post_validate_singleton_llm(
        parsed: SingletonRefactorLLMResponse,
    ) -> SingletonRefactorLLMResponse:
        nonlocal validated_prepared
        artifact: dict[str, Any] = {"llm_response": parsed.model_dump()}
        attempt_artifacts.append(artifact)
        raise_if_llm_declared_error(
            parsed,
            kind="singleton",
            target=failure_target,
        )
        prepared = prepare_singleton_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        artifact["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_singleton_refactor_validation(prepared)
        return parsed

    def _post_validate_singleton_naming(parsed):
        from src.mechanical_naming import apply_cluster_naming_response

        nonlocal validated_prepared
        assert mechanical_draft is not None
        artifact: dict[str, Any] = {"llm_response": parsed.model_dump()}
        attempt_artifacts.append(artifact)
        raise_if_llm_declared_error(
            parsed,
            kind="singleton",
            target=failure_target,
        )
        body = apply_cluster_naming_response(
            parsed,
            mechanical_draft,
            parameter_names=frozenset(),
            forbidden_names=existing_names | frozenset(ctx.allowed_runtime_symbols),
        )
        legacy_shape = SingletonRefactorLLMResponse(
            symbol_docstring=parsed.symbol_docstring,
            symbol_body=body,
            error=None,
            error_reason=None,
        )
        prepared = prepare_singleton_refactor_response(
            legacy_shape,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        artifact["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_singleton_refactor_validation(prepared)
        return parsed

    logger.info(
        "singleton refactor LLM request address=%s model=%s prompt_version=%s "
        "contract=%s",
        ctx.address,
        model,
        REFACTOR_PROMPT_VERSION,
        "naming_only" if mechanical_draft is not None else "full_body",
    )
    try:
        if mechanical_draft is not None:
            from src.mechanical_naming import SingletonNamingLLMResponse

            _naming_parsed, raw_content = generate_validated_json(
                client=client,
                model=model,
                provider=provider,
                system_prompt=(
                    "You add the semantic layer (docstring and local names) to a "
                    "verified, mechanically generated Python function. Return only "
                    "JSON matching the schema."
                ),
                user_prompt=user_prompt,
                response_model=SingletonNamingLLMResponse,
                post_validate=_post_validate_singleton_naming,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
            )
            llm_parsed = None
        else:
            llm_parsed, raw_content = generate_validated_json(
                client=client,
                model=model,
                provider=provider,
                system_prompt=(
                    "You rename and refactor one Excel-generated singleton helper "
                    "into a domain-aware semantic function. Return only JSON "
                    "matching the schema."
                ),
                user_prompt=user_prompt,
                response_model=SingletonRefactorLLMResponse,
                post_validate=_post_validate_singleton_llm,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
            )
    except RefactorDeclaredError as error:
        last_artifact = attempt_artifacts[-1] if attempt_artifacts else {}
        dump_dir = write_refactor_failure_diagnostic(
            kind="singleton",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_artifact.get("llm_response"),
            prepared_response=last_artifact.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "singleton refactor aborted by LLM address=%s reason=%s diagnostic=%s",
            ctx.address,
            error.reason,
            dump_dir,
        )
        raise
    except ValidatedJsonFailure as error:
        dump_dir = _dump_validated_json_failure(
            kind="singleton",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            local_artifacts=attempt_artifacts,
            model=model,
        )
        logger.error(
            "singleton refactor LLM request failed address=%s attempts=%s diagnostic=%s: %s",
            ctx.address,
            DEFAULT_MAX_ATTEMPTS,
            dump_dir,
            error,
        )
        raise
    except RuntimeError as error:
        last_artifact = attempt_artifacts[-1] if attempt_artifacts else {}
        dump_dir = write_refactor_failure_diagnostic(
            kind="singleton",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_artifact.get("llm_response"),
            prepared_response=last_artifact.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "singleton refactor LLM request failed address=%s attempts=%s diagnostic=%s: %s",
            ctx.address,
            DEFAULT_MAX_ATTEMPTS,
            dump_dir,
            error,
        )
        raise
    if validated_prepared is not None:
        parsed = validated_prepared
    else:
        if llm_parsed is None:
            raise RuntimeError(
                "naming-contract response completed without a validated refactor"
            )
        parsed = _finalize_singleton_from_llm(llm_parsed)
    _ = raw_content
    cache[cache_key] = parsed.model_dump_json()
    save_refactor_cache(cache)
    return parsed


def _mechanical_bodies_enabled() -> bool:
    value = os.environ.get("MECHANICAL_REFACTOR_BODIES", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _synthesize_key_dispatch_cluster_body(
    ctx: ClusterRefactorContext,
) -> MechanicalBodyDraft:
    """Assemble a key-dispatch body from per-regime mechanical drafts."""
    from src.key_dispatch_synthesis import (
        is_difference_composition_formula,
        regime_callee_key,
        synthesize_key_dispatch_body,
    )
    from src.mechanical_body import MechanicalSynthesisError, synthesize_cluster_body

    plan = ctx.key_dispatch_plan
    if plan is None:
        raise MechanicalSynthesisError("missing_key_dispatch_plan")
    bound_keys = ctx.key_dispatch_bound_keys
    if bound_keys is None:
        raise MechanicalSynthesisError("missing_key_dispatch_bound_keys")

    members_by_address = {member.address: member for member in ctx.members}
    regime_callees: dict[tuple[tuple[str, BindingKeyValue], ...], str] = {}
    regime_bodies: dict[tuple[tuple[str, BindingKeyValue], ...], str] = {}
    renameable: list[str] = []
    lookup_tables: list[str] = []
    semantic_refs = tuple(
        SemanticDependencyRef(
            helper_name=dependency.helper_name,
            call_form=dependency.call_form,
            address_template=dependency.address_template,
            addresses=dependency.addresses,
        )
        for dependency in ctx.semantic_dependencies
    )

    for regime in plan.regimes:
        key = regime_callee_key(regime.dispatch_key_values)
        if is_difference_composition_formula(regime.canonical_formula):
            regime_callees[key] = ""
            continue
        regime_members = [
            members_by_address[address]
            for address in regime.members
            if address in members_by_address
        ]
        if len(regime_members) != len(regime.members):
            missing = sorted(set(regime.members) - set(members_by_address))
            raise MechanicalSynthesisError(
                "key_dispatch_partial_regime_members:"
                f"{regime.dispatch_key_values!r}:missing={missing}"
            )
        if not plan.sweep_dimension_ids:
            raise MechanicalSynthesisError("key_dispatch_regime_without_sweep_dims")
        sweep_keys = {
            address: {
                dimension_id: ctx.expected_member_keys[address][dimension_id]
                for dimension_id in plan.sweep_dimension_ids
            }
            for address in regime.members
        }
        sweep_vocabulary = tuple(
            spec
            for spec in ctx.key_vocabulary
            if spec.dimension_id in plan.sweep_dimension_ids
        )
        summary = build_cluster_fingerprint_summary(
            regime_members,
            expected_member_keys=sweep_keys,
            bound_address_keys=bound_keys,
            workbook_path=None,
            semantic_dependencies=semantic_refs,
        )
        draft = synthesize_cluster_body(
            summary,
            key_vocabulary=sweep_vocabulary,
            expected_member_keys=sweep_keys,
            helper_name=ctx.expected_helper_name,
        )
        regime_bodies[key] = draft.body
        renameable.extend(draft.renameable_locals)
        lookup_tables.extend(draft.lookup_table_names)

    body = synthesize_key_dispatch_body(
        plan,
        regime_callees=regime_callees,
        regime_bodies=regime_bodies,
        include_ctx=True,
    )
    return MechanicalBodyDraft(
        body=body,
        renameable_locals=tuple(dict.fromkeys(renameable)),
        lookup_table_names=tuple(dict.fromkeys(lookup_tables)),
        group_count=len(plan.regimes),
    )


def _try_synthesize_cluster_body(ctx: ClusterRefactorContext):
    """Return a verified mechanical body draft, or None to use the legacy contract."""
    if not _mechanical_bodies_enabled():
        return None
    from src.mechanical_body import MechanicalSynthesisError, synthesize_cluster_body

    if ctx.contract == "key_dispatch":
        try:
            draft = _synthesize_key_dispatch_cluster_body(ctx)
        except (MechanicalSynthesisError, ValueError) as error:
            reason = (
                error.reason
                if isinstance(error, MechanicalSynthesisError)
                else str(error)
            )
            logger.info(
                "cluster %s key-dispatch mechanical synthesis unavailable (%s); "
                "using full-body contract",
                ctx.cluster_id,
                reason,
            )
            return None
        logger.info(
            "cluster %s key-dispatch mechanical draft verified; "
            "using naming-only contract",
            ctx.cluster_id,
        )
        return draft

    summary = ctx.fingerprint_summary
    if summary is None or summary.fallback_reason is not None:
        return None

    try:
        draft = synthesize_cluster_body(
            summary,
            key_vocabulary=ctx.key_vocabulary,
            expected_member_keys=ctx.expected_member_keys,
            helper_name=ctx.expected_helper_name,
        )
    except MechanicalSynthesisError as error:
        logger.info(
            "cluster %s mechanical synthesis unavailable (%s); "
            "using full-body contract",
            ctx.cluster_id,
            error.reason,
        )
        return None
    logger.info(
        "cluster %s mechanical draft verified; using naming-only contract",
        ctx.cluster_id,
    )
    return draft


def _try_synthesize_singleton_body(ctx: SingletonRefactorContext):
    """Return a mechanical singleton body draft, or None to use the legacy contract."""
    if not _mechanical_bodies_enabled():
        return None
    from src.mechanical_body import (
        MechanicalSynthesisError,
        synthesize_singleton_body,
    )

    try:
        draft = synthesize_singleton_body(
            ctx.python_source,
            inline_replacements=dict(ctx.inline_replacements),
        )
    except MechanicalSynthesisError as error:
        logger.info(
            "singleton %s mechanical synthesis unavailable (%s); "
            "using full-body contract",
            ctx.address,
            error.reason,
        )
        return None
    logger.info(
        "singleton %s mechanical draft assembled; using naming-only contract",
        ctx.address,
    )
    return draft


def llm_refactor_cluster(
    ctx: ClusterRefactorContext,
    *,
    internals_path: Path,
    pristine_source: str | None = None,
    input_vectors: Sequence[Mapping[str, object]] | None = None,
    source_graph: DependencyGraph | None = None,
    diagnostic_target: str | None = None,
    internals_index: InternalsSourceIndex | None = None,
) -> ClusterRefactorResponse:
    mechanical_draft = _try_synthesize_cluster_body(ctx)
    if mechanical_draft is not None:
        from src.mechanical_naming import ClusterNamingLLMResponse

        llm_schema: dict[str, object] = {
            "schema": ClusterNamingLLMResponse.model_json_schema(),
            "mechanical_body": mechanical_draft.body,
        }
    else:
        llm_schema = ClusterRefactorLLMResponse.model_json_schema()
    index = _resolve_internals_index(internals_path, internals_index=internals_index)
    internals_bytes = index.source.encode("utf-8")
    internals_source = index.source
    runtime_source = _read_runtime_source(internals_path)
    existing_names = _function_names(internals_source, index=index)
    cache = load_refactor_cache()
    cache_key = refactor_cache_key(ctx, internals_bytes, llm_schema)
    failure_target = diagnostic_target or f"cluster_{ctx.cluster_id}"
    if _CLUSTER_CONTEXT_OBSERVER is not None:
        _CLUSTER_CONTEXT_OBSERVER(ctx)
    if _PROMPT_OBSERVER is not None:
        _PROMPT_OBSERVER(
            "cluster",
            failure_target,
            _prompt_for_refactor(
                build_cluster_refactor_prompt_context(
                    ctx,
                    internals_path=internals_path,
                    internals_index=index,
                ),
                contract=ctx.contract,
            ),
        )

    def _apply_cluster_refactor_validation(
        prepared: ClusterRefactorResponse,
    ) -> ClusterRefactorResponse:
        prepared = _prepare_cluster_refactor_response(prepared, ctx)
        validate_cluster_refactor_response(
            ctx,
            prepared,
            existing_names=existing_names,
            internals_source=internals_source,
        )
        if pristine_source is not None and input_vectors is not None:
            from src.refactor_parity_gate import check_cluster_parity

            check_cluster_parity(
                pristine_source=pristine_source,
                current_source=internals_source,
                response=prepared,
                ctx=ctx,
                input_vectors=input_vectors,
                package_root=internals_path.parent,
            )
        return prepared

    def _finalize_cluster_from_llm(
        parsed: ClusterRefactorLLMResponse,
    ) -> ClusterRefactorResponse:
        prepared = prepare_cluster_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        return _apply_cluster_refactor_validation(prepared)

    def _validate_cached_cluster_response(
        cached_response: ClusterRefactorResponse,
    ) -> ClusterRefactorResponse:
        try:
            return _apply_cluster_refactor_validation(cached_response)
        except Exception as error:
            dump_dir = write_refactor_failure_diagnostic(
                kind="cluster",
                target=failure_target,
                error=error,
                llm_response=cached_response.model_dump(),
                prepared_response=cached_response.model_dump(),
                source="cache",
            )
            logger.error(
                "cluster refactor failed cluster_id=%s source=cache diagnostic=%s: %s",
                ctx.cluster_id,
                dump_dir,
                error,
            )
            raise

    cached_content = cache.get(cache_key)
    if cached_content is not None:
        try:
            logger.info(
                "cluster refactor cache hit cluster_id=%s key=%s",
                ctx.cluster_id,
                cache_key[:12],
            )
            return _validate_cached_cluster_response(
                ClusterRefactorResponse.model_validate_json(cached_content)
            )
        except (ValueError, ValidationError) as error:
            if not _refactor_provider_key_present():
                raise
            logger.warning(
                "cluster refactor cache stale cluster_id=%s key=%s: %s",
                ctx.cluster_id,
                cache_key[:12],
                error,
            )
            del cache[cache_key]
            save_refactor_cache(cache)

    model = refactor_model()
    client, provider = build_client(model)
    context_dump = build_cluster_refactor_prompt_context(
        ctx,
        internals_path=internals_path,
        internals_index=index,
    )
    if mechanical_draft is not None:
        from src.mechanical_naming import (
            format_cluster_naming_prompt_context,
            load_cluster_naming_prompt_fixed_portion,
        )

        user_prompt = (
            load_cluster_naming_prompt_fixed_portion().strip()
            + "\n\n"
            + format_cluster_naming_prompt_context(context_dump, mechanical_draft)
        )
    else:
        user_prompt = _prompt_for_refactor(context_dump, contract=ctx.contract)
    attempt_artifacts: list[dict[str, Any]] = []
    validated_prepared: ClusterRefactorResponse | None = None

    def _post_validate_cluster_llm(
        parsed: ClusterRefactorLLMResponse,
    ) -> ClusterRefactorLLMResponse:
        nonlocal validated_prepared
        artifact: dict[str, Any] = {"llm_response": parsed.model_dump()}
        attempt_artifacts.append(artifact)
        raise_if_llm_declared_error(
            parsed,
            kind="cluster",
            target=failure_target,
        )
        prepared = prepare_cluster_refactor_response(
            parsed,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        artifact["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_cluster_refactor_validation(prepared)
        return parsed

    def _post_validate_cluster_naming(parsed):
        from src.mechanical_naming import apply_cluster_naming_response

        nonlocal validated_prepared
        assert mechanical_draft is not None
        artifact: dict[str, Any] = {"llm_response": parsed.model_dump()}
        attempt_artifacts.append(artifact)
        raise_if_llm_declared_error(
            parsed,
            kind="cluster",
            target=failure_target,
        )
        body = apply_cluster_naming_response(
            parsed,
            mechanical_draft,
            parameter_names=frozenset(
                parameter.name for parameter in synthesize_cluster_parameters(ctx)
            ),
            forbidden_names=existing_names | frozenset(ctx.allowed_runtime_symbols),
        )
        legacy_shape = ClusterRefactorLLMResponse(
            symbol_docstring=parsed.symbol_docstring,
            symbol_body=body,
            error=None,
            error_reason=None,
        )
        prepared = prepare_cluster_refactor_response(
            legacy_shape,
            ctx,
            runtime_source=runtime_source,
            internals_source=internals_source,
        )
        artifact["prepared_response"] = prepared.model_dump()
        validated_prepared = _apply_cluster_refactor_validation(prepared)
        return parsed

    prompt_member_count = len(
        sample_indices_for_prompt(
            len(ctx.members), limit=CLUSTER_REFACTOR_PROMPT_MEMBER_LIMIT
        )
    )
    logger.info(
        "cluster refactor LLM request cluster_id=%s members=%d prompt_members=%d "
        "model=%s prompt_version=%s contract=%s",
        ctx.cluster_id,
        len(ctx.members),
        prompt_member_count,
        model,
        REFACTOR_PROMPT_VERSION,
        "naming_only" if mechanical_draft is not None else ctx.contract,
    )
    try:
        if mechanical_draft is not None:
            from src.mechanical_naming import ClusterNamingLLMResponse

            _naming_parsed, raw_content = generate_validated_json(
                client=client,
                model=model,
                provider=provider,
                system_prompt=(
                    "You add the semantic layer (docstring and local names) to a "
                    "verified, mechanically generated Python function. Return only "
                    "JSON matching the schema."
                ),
                user_prompt=user_prompt,
                response_model=ClusterNamingLLMResponse,
                post_validate=_post_validate_cluster_naming,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
            )
            llm_parsed = None
        else:
            llm_parsed, raw_content = generate_validated_json(
                client=client,
                model=model,
                provider=provider,
                system_prompt=(
                    "You refactor parallel Excel-generated Python helpers into one "
                    "domain-aware parameterized function. Return only JSON matching the schema."
                ),
                user_prompt=user_prompt,
                response_model=ClusterRefactorLLMResponse,
                post_validate=_post_validate_cluster_llm,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
            )
    except RefactorDeclaredError as error:
        last_artifact = attempt_artifacts[-1] if attempt_artifacts else {}
        dump_dir = write_refactor_failure_diagnostic(
            kind="cluster",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_artifact.get("llm_response"),
            prepared_response=last_artifact.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "cluster refactor aborted by LLM cluster_id=%s reason=%s diagnostic=%s",
            ctx.cluster_id,
            error.reason,
            dump_dir,
        )
        raise
    except ValidatedJsonFailure as error:
        dump_dir = _dump_validated_json_failure(
            kind="cluster",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            local_artifacts=attempt_artifacts,
            model=model,
        )
        logger.error(
            "cluster refactor LLM request failed cluster_id=%s attempts=%s diagnostic=%s: %s",
            ctx.cluster_id,
            DEFAULT_MAX_ATTEMPTS,
            dump_dir,
            error,
        )
        raise
    except RuntimeError as error:
        last_artifact = attempt_artifacts[-1] if attempt_artifacts else {}
        dump_dir = write_refactor_failure_diagnostic(
            kind="cluster",
            target=failure_target,
            error=error,
            user_prompt=user_prompt,
            llm_response=last_artifact.get("llm_response"),
            prepared_response=last_artifact.get("prepared_response"),
            source="llm",
            model=model,
        )
        logger.error(
            "cluster refactor LLM request failed cluster_id=%s attempts=%s diagnostic=%s: %s",
            ctx.cluster_id,
            DEFAULT_MAX_ATTEMPTS,
            dump_dir,
            error,
        )
        raise
    if validated_prepared is not None:
        parsed = validated_prepared
    else:
        if llm_parsed is None:
            raise RuntimeError(
                "naming-contract response completed without a validated refactor"
            )
        parsed = _finalize_cluster_from_llm(llm_parsed)
    _ = raw_content
    cache[cache_key] = parsed.model_dump_json()
    save_refactor_cache(cache)
    return parsed


def _prompt_for_singleton_refactor(
    payload_or_context: dict[str, object] | str,
    response_schema: dict[str, object] | None = None,
) -> str:
    _ = response_schema
    fixed = load_singleton_refactor_prompt_fixed_portion().strip()
    if isinstance(payload_or_context, str):
        return f"{fixed}\n\n{payload_or_context.strip()}"
    payload_json = json.dumps(payload_or_context, indent=2, default=str)
    return f"{fixed}\n\nSingleton context:\n{payload_json}"


def _prompt_for_refactor(
    payload_or_context: dict[str, object] | str,
    response_schema: dict[str, object] | None = None,
    *,
    contract: ClusterRefactorContract = "member_sweep",
) -> str:
    _ = response_schema
    fixed = load_cluster_refactor_prompt_fixed_portion(contract).strip()
    if isinstance(payload_or_context, str):
        return f"{fixed}\n\n{payload_or_context.strip()}"
    payload_json = json.dumps(payload_or_context, indent=2, default=str)
    return f"{fixed}\n\nCluster context:\n{payload_json}"


class _CallSiteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        caller_function: str,
        caller_address: str | None,
        member_addresses: frozenset[str],
        member_functions: set[str],
        lines: list[str],
        sites: list[CallSite],
    ) -> None:
        self.caller_function = caller_function
        self.caller_address = caller_address
        self.member_addresses = member_addresses
        self.member_functions = member_functions
        self.lines = lines
        self.sites = sites

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "xl_eval"
            and len(node.args) >= 3
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and isinstance(node.args[2], ast.Name)
        ):
            address = node.args[1].value
            callee_function = node.args[2].id
            if (
                address in self.member_addresses
                and callee_function in self.member_functions
            ):
                self._append_site(
                    callee_function=callee_function,
                    callee_address=address,
                    pattern="xl_eval",
                    line=node.lineno,
                )
        elif isinstance(node.func, ast.Name) and node.func.id in self.member_functions:
            callee_function = node.func.id
            callee_address = _function_to_primary_address(
                callee_function,
                self.member_addresses,
            )
            if callee_address is not None:
                self._append_site(
                    callee_function=callee_function,
                    callee_address=callee_address,
                    pattern="direct",
                    line=node.lineno,
                )
        self.generic_visit(node)

    def _append_site(
        self,
        *,
        callee_function: str,
        callee_address: str,
        pattern: Literal["xl_eval", "direct"],
        line: int,
    ) -> None:
        snippet = self.lines[line - 1].strip()
        self.sites.append(
            CallSite(
                caller_function=self.caller_function,
                caller_address=self.caller_address,
                callee_function=callee_function,
                callee_address=callee_address,
                pattern=pattern,
                line=line,
                snippet=snippet,
            )
        )


def _caller_address(function_name: str) -> str | None:
    if not function_name.startswith("cell_"):
        return None
    remainder = function_name.removeprefix("cell_")
    parts = remainder.split("_", 1)
    if len(parts) != 2:
        return None
    sheet, colrow = parts[0], parts[1]
    return f"{sheet.title()}!{colrow.upper()}"


def _function_to_primary_address(
    function_name: str,
    member_addresses: frozenset[str],
) -> str | None:
    matches = [
        address
        for address in member_addresses
        if address_to_function_name(address) == function_name
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _function_names(
    source: str,
    *,
    index: InternalsSourceIndex | None = None,
) -> frozenset[str]:
    if index is not None:
        return frozenset(index.functions)
    module = ast.parse(source)
    return frozenset(
        node.name for node in module.body if isinstance(node, ast.FunctionDef)
    )
