"""Acceptance tests for the two-pass mechanical / semantic refactor split (#166/#169)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent
from typing import cast

import pytest

from src.internals_refactor import (
    ClusterRefactorApplyResult,
    ClusterRefactorContext,
    ClusterRefactorResponse,
    MemberContext,
    _PendingSemanticUnit,
    build_mechanical_cluster_response,
    extract_function_source,
    mechanical_placeholder_docstring,
    write_refactor_failure_diagnostic,
)
from src.mechanical_body import MechanicalBodyDraft
from src.mechanical_naming import (
    ClusterNamingLLMResponse,
    LocalRename,
    NamingUnit,
    apply_cluster_naming_response,
    apply_naming_responses_to_module,
)
from src.refactor_bindings import KeyConceptSpec

# Runtime allowlist used by CLUSTER_CONTEXT fixtures in this module.
_ALLOWED = (
    "XlError",
    "xl_cell",
    "xl_eval",
    "xl_number",
)


def _draft_a() -> MechanicalBodyDraft:
    return MechanicalBodyDraft(
        body="_t1 = xl_number(ctx)\nreturn _t1",
        renameable_locals=("_t1",),
        lookup_table_names=(),
        group_count=1,
    )


def _draft_b() -> MechanicalBodyDraft:
    return MechanicalBodyDraft(
        body="_t1 = xl_bool(ctx)\nreturn _t1",
        renameable_locals=("_t1",),
        lookup_table_names=(),
        group_count=1,
    )


def _naming(
    original: str, replacement: str, *, summary: str
) -> ClusterNamingLLMResponse:
    return ClusterNamingLLMResponse(
        symbol_docstring=(
            f"{summary}\n\nArgs:\n    ctx: Context.\n\nReturns:\n    Value."
        ),
        renames=(LocalRename(original=original, replacement=replacement),),
        error=None,
        error_reason=None,
    )


def _mechanical_module() -> str:
    return dedent(
        '''
        from __future__ import annotations

        def helper_a(ctx):
            """Mechanically synthesized helper.

            Args:
                ctx: Workbook evaluation context.

            Returns:
                Cell value.
            """
            _t1 = xl_number(ctx)
            return _t1

        def helper_b(ctx):
            """Mechanically synthesized helper.

            Args:
                ctx: Workbook evaluation context.

            Returns:
                Cell value.
            """
            _t1 = xl_bool(ctx)
            return _t1
        '''
    ).strip()


def test_applying_two_naming_responses_is_order_independent() -> None:
    """Pass-2 applier: both application orders yield identical source (#166)."""
    module = _mechanical_module()
    response_a = _naming("_t1", "numeric_value", summary="Numeric helper.")
    response_b = _naming("_t1", "flag_value", summary="Boolean helper.")
    forbidden = frozenset({"xl_number", "xl_bool", "helper_a", "helper_b"})
    units = (
        NamingUnit(
            helper_name="helper_a",
            draft=_draft_a(),
            response=response_a,
            parameter_names=frozenset(),
            forbidden_names=forbidden,
        ),
        NamingUnit(
            helper_name="helper_b",
            draft=_draft_b(),
            response=response_b,
            parameter_names=frozenset(),
            forbidden_names=forbidden,
        ),
    )
    forward = apply_naming_responses_to_module(module, units)
    reverse = apply_naming_responses_to_module(module, tuple(reversed(units)))
    assert forward == reverse
    assert "numeric_value = xl_number(ctx)" in forward
    assert "flag_value = xl_bool(ctx)" in forward
    assert "_t1" not in forward


def test_semantic_naming_cache_payload_omits_internals_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.internals_refactor import semantic_naming_cache_payload

    monkeypatch.setattr("src.internals_refactor.refactor_model", lambda: "test-model")
    payload = semantic_naming_cache_payload(
        kind="cluster",
        unit_id="cluster:1",
        canonical_template="=A1",
        mechanical_body="_t1 = 1\nreturn _t1",
        response_schema={"type": "object"},
        member_fingerprints=(("Engine!C1", "aaa", "bbb"),),
        contract="member_sweep",
    )
    assert "internals_sha256" not in payload
    assert payload["mechanical_body_sha256"]
    assert payload["kind"] == "cluster"


def test_semantic_naming_cache_key_changes_with_mechanical_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.internals_refactor import semantic_naming_cache_key

    monkeypatch.setattr("src.internals_refactor.refactor_model", lambda: "test-model")
    key_a = semantic_naming_cache_key(
        kind="cluster",
        unit_id="cluster:1",
        canonical_template="=A1",
        mechanical_body="_t1 = 1\nreturn _t1",
        response_schema={"type": "object"},
        member_fingerprints=(("Engine!C1", "aaa", "bbb"),),
        contract="member_sweep",
    )
    key_b = semantic_naming_cache_key(
        kind="cluster",
        unit_id="cluster:1",
        canonical_template="=A1",
        mechanical_body="_t1 = 2\nreturn _t1",
        response_schema={"type": "object"},
        member_fingerprints=(("Engine!C1", "aaa", "bbb"),),
        contract="member_sweep",
    )
    assert key_a != key_b


def test_write_refactor_failure_diagnostic_uses_unit_identity_dirname(
    tmp_path: Path,
) -> None:
    dump_dir = write_refactor_failure_diagnostic(
        kind="cluster",
        target="cluster_42",
        error=ValueError("boom"),
        dump_dir=tmp_path,
        source="llm",
        model="test-model",
    )
    assert dump_dir.parent == tmp_path
    assert dump_dir.name == "cluster_42"
    assert not re.match(r"^\d{8}T\d{6}Z_", dump_dir.name)


def test_prompt_dump_observer_names_by_unit_identity(tmp_path: Path) -> None:
    from scripts.run_refactor_stage import _prompt_dump_observer

    observe = _prompt_dump_observer(tmp_path)
    observe("cluster", "helper_alpha", "prompt a")
    observe("singleton", "helper_beta", "prompt b")
    observe("cluster", "helper_alpha", "prompt a again")
    names = sorted(path.name for path in tmp_path.glob("*.md"))
    assert names == ["cluster_helper_alpha.md", "singleton_helper_beta.md"]
    assert (tmp_path / "cluster_helper_alpha.md").read_text(encoding="utf-8") == (
        "prompt a again"
    )


def test_apply_cluster_naming_response_leaves_other_helpers_untouched() -> None:
    body = apply_cluster_naming_response(
        _naming("_t1", "numeric_value", summary="Numeric helper."),
        _draft_a(),
        parameter_names=frozenset(),
        forbidden_names=frozenset({"xl_number"}),
    )
    assert body == "numeric_value = xl_number(ctx)\nreturn numeric_value"


def _cluster_passthrough_context() -> ClusterRefactorContext:
    members = (
        MemberContext(
            address="Engine!C6",
            function_name="cell_engine_c6",
            engine_column="C",
            normalized_formula="=Inputs!C1",
            python_source=(
                "def cell_engine_c6(ctx):\n    return xl_cell(ctx, 'Inputs!C1')\n"
            ),
            dependency_addresses=(),
            dependency_functions=(),
        ),
        MemberContext(
            address="Engine!D6",
            function_name="cell_engine_d6",
            engine_column="D",
            normalized_formula="=Inputs!D1",
            python_source=(
                "def cell_engine_d6(ctx):\n    return xl_cell(ctx, 'Inputs!D1')\n"
            ),
            dependency_addresses=(),
            dependency_functions=(),
        ),
    )
    return ClusterRefactorContext(
        cluster_id=1,
        canonical_template="=Inputs!{col}1",
        row=6,
        members=members,
        external_dependencies=(),
        semantic_dependencies=(),
        call_sites=(),
        first_year_column="C",
        allowed_runtime_symbols=_ALLOWED,
        key_vocabulary=(
            KeyConceptSpec(
                dimension_id="TIME_PERIOD",
                concept="TIME_PERIOD",
                dtype="int",
                suggested_param_name="time_period",
            ),
        ),
        expected_member_keys={
            "Engine!C6": {"TIME_PERIOD": 1},
            "Engine!D6": {"TIME_PERIOD": 2},
        },
        naming_hints={},
        expected_helper_name="combined_input_passthrough",
    )


def _cluster_mechanical_draft() -> MechanicalBodyDraft:
    return MechanicalBodyDraft(
        body=(
            "_t1 = {1: 'C', 2: 'D'}\n"
            "_t2 = _t1[time_period]\n"
            "return xl_cell(ctx, f'Inputs!{_t2}1')"
        ),
        renameable_locals=("_t1", "_t2"),
        lookup_table_names=(),
        group_count=1,
    )


def _cluster_naming_response() -> ClusterNamingLLMResponse:
    return ClusterNamingLLMResponse(
        symbol_docstring=(
            "Return the passthrough input for a projection period.\n\n"
            "Args:\n"
            "    ctx: Workbook evaluation context.\n"
            "    time_period: Projection period.\n\n"
            "Returns:\n"
            "    The corresponding input value."
        ),
        renames=(
            LocalRename(original="_t1", replacement="columns_by_period"),
            LocalRename(original="_t2", replacement="column"),
        ),
        error=None,
        error_reason=None,
    )


def test_apply_and_validate_semantic_naming_uses_module_applier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live pass-2 batch apply must call apply_naming_responses_to_module (#169)."""
    from src import internals_refactor as module
    from src.mechanical_naming import (
        apply_naming_responses_to_module as real_apply,
    )

    calls: list[object] = []

    def tracking_apply(
        source: str, units: Sequence[NamingUnit], **kwargs: object
    ) -> str:
        calls.append(units)
        return real_apply(source, units)

    monkeypatch.setattr(
        "src.mechanical_naming.apply_naming_responses_to_module",
        tracking_apply,
    )

    ctx = _cluster_passthrough_context()
    draft = _cluster_mechanical_draft()
    runtime_source = "def xl_cell(ctx, address): ...\n"
    mechanical = build_mechanical_cluster_response(
        ctx,
        draft,
        runtime_source=runtime_source,
        internals_source="def cell_engine_c6(ctx):\n    return 1\n",
    )
    source = (
        "from __future__ import annotations\n\n"
        + mechanical.helper_source.strip()
        + "\n\n"
        + "def cell_engine_c6(ctx):\n"
        + "    return combined_input_passthrough(ctx, time_period=1)\n\n"
        + "def cell_engine_d6(ctx):\n"
        + "    return combined_input_passthrough(ctx, time_period=2)\n"
    )

    pending = module._PendingSemanticUnit(
        kind="cluster",
        unit_id="cluster:1",
        helper_name=mechanical.helper_name,
        diagnostic_target="cluster:1",
        canonical_template=ctx.canonical_template,
        contract=ctx.contract,
        draft=draft,
        ctx=ctx,
        parameter_names=frozenset(p.name for p in mechanical.parameters),
        forbidden_names=frozenset(
            {"combined_input_passthrough", "xl_cell", "cell_engine_c6"}
        ),
        member_fingerprints=(),
        member_checks=(),
    )
    named_source, prepared, skipped = module._apply_and_validate_semantic_naming(
        (pending,),
        {"cluster:1": _cluster_naming_response()},
        source,
        runtime_source=runtime_source,
    )

    assert skipped == []
    assert calls, "pass 2 must apply via apply_naming_responses_to_module"
    assert "columns_by_period" in named_source
    assert "Mechanically synthesized" not in named_source
    assert "cluster:1" in prepared
    cluster_prepared = cast(module.ClusterRefactorResponse, prepared["cluster:1"])
    assert "Mechanically synthesized" not in cluster_prepared.helper_docstring
    assert "columns_by_period" in cluster_prepared.helper_source
    assert extract_function_source(
        named_source, "combined_input_passthrough"
    ).strip() == (cluster_prepared.helper_source.strip())


def test_refresh_mechanical_cluster_results_replaces_placeholder_response() -> None:
    """Returned ClusterRefactorApplyResult must reflect post-naming helpers (#169)."""
    from src import internals_refactor as module

    ctx = _cluster_passthrough_context()
    draft = _cluster_mechanical_draft()
    runtime_source = "def xl_cell(ctx, address): ...\n"
    mechanical = build_mechanical_cluster_response(
        ctx,
        draft,
        runtime_source=runtime_source,
        internals_source="def cell_engine_c6(ctx):\n    return 1\n",
    )
    assert (
        mechanical_placeholder_docstring(["time_period"]) in mechanical.helper_docstring
    )

    pending = module._PendingSemanticUnit(
        kind="cluster",
        unit_id="cluster:1",
        helper_name=mechanical.helper_name,
        diagnostic_target="cluster:1",
        canonical_template=ctx.canonical_template,
        contract=ctx.contract,
        draft=draft,
        ctx=ctx,
        parameter_names=frozenset(p.name for p in mechanical.parameters),
        forbidden_names=frozenset({"combined_input_passthrough", "xl_cell"}),
        member_fingerprints=(),
        member_checks=(),
    )
    source = (
        "from __future__ import annotations\n\n"
        + mechanical.helper_source.strip()
        + "\n"
    )
    named_source, prepared, skipped = module._apply_and_validate_semantic_naming(
        (pending,),
        {"cluster:1": _cluster_naming_response()},
        source,
        runtime_source=runtime_source,
    )
    assert skipped == []
    results = [
        ClusterRefactorApplyResult(
            source=source,
            helper_name=mechanical.helper_name,
            wrappers_applied=("cell_engine_c6", "cell_engine_d6"),
            dry_run=True,
            response=mechanical,
        )
    ]
    refreshed = module._refresh_mechanical_cluster_results(
        results,
        pending_units=(pending,),
        prepared_by_unit_id=prepared,
        named_source=named_source,
    )
    assert len(refreshed) == 1
    assert refreshed[0].source == named_source
    assert "Mechanically synthesized" not in refreshed[0].response.helper_docstring
    assert "columns_by_period" in refreshed[0].response.helper_source
    assert refreshed[0].wrappers_applied == ("cell_engine_c6", "cell_engine_d6")


def _pending_passthrough_unit(
    *,
    unit_id: str = "cluster:1",
) -> tuple[_PendingSemanticUnit, ClusterRefactorResponse, str, str]:
    """Return (pending, mechanical_response, source, runtime_source)."""
    ctx = _cluster_passthrough_context()
    draft = _cluster_mechanical_draft()
    runtime_source = "def xl_cell(ctx, address): ...\n"
    mechanical = build_mechanical_cluster_response(
        ctx,
        draft,
        runtime_source=runtime_source,
        internals_source="def cell_engine_c6(ctx):\n    return 1\n",
    )
    source = (
        "from __future__ import annotations\n\n"
        + mechanical.helper_source.strip()
        + "\n\n"
        + "def cell_engine_c6(ctx):\n"
        + "    return combined_input_passthrough(ctx, time_period=1)\n\n"
        + "def cell_engine_d6(ctx):\n"
        + "    return combined_input_passthrough(ctx, time_period=2)\n"
    )
    pending = _PendingSemanticUnit(
        kind="cluster",
        unit_id=unit_id,
        helper_name=mechanical.helper_name,
        diagnostic_target=unit_id,
        canonical_template=ctx.canonical_template,
        contract=ctx.contract,
        draft=draft,
        ctx=ctx,
        parameter_names=frozenset(p.name for p in mechanical.parameters),
        forbidden_names=frozenset(
            {
                "combined_input_passthrough",
                "xl_cell",
                "cell_engine_c6",
                "cell_engine_d6",
            }
        ),
        member_fingerprints=(),
        member_checks=(),
    )
    return pending, mechanical, source, runtime_source


def test_apply_and_validate_semantic_naming_skips_unit_on_validate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply-phase validate failure leaves that unit unapplied (#skip naming)."""
    from src import internals_refactor as module

    pending, _mechanical, source, runtime_source = _pending_passthrough_unit()
    real_validate = module.validate_cluster_refactor_response

    def boom_validate(ctx, prepared, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("synthetic apply validate failure")

    monkeypatch.setattr(module, "validate_cluster_refactor_response", boom_validate)

    named_source, prepared, skipped = module._apply_and_validate_semantic_naming(
        (pending,),
        {pending.unit_id: _cluster_naming_response()},
        source,
        runtime_source=runtime_source,
    )

    assert named_source == source
    assert prepared == {}
    assert len(skipped) == 1
    assert skipped[0][0] == pending.unit_id
    assert skipped[0][1] == pending.helper_name
    assert "synthetic apply validate failure" in str(skipped[0][2])
    assert "Mechanically synthesized helper pending semantic naming." in named_source
    # Restore sanity: real validate still works outside this test's patch.
    assert real_validate is not None


def test_run_semantic_naming_pass_skips_gather_failure_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gather failure keeps the mechanical helper and warns with CLI retry hint."""
    import logging

    from src import internals_refactor as module

    pending, _mechanical, source, runtime_source = _pending_passthrough_unit()
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(module, "REFACTOR_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(module, "load_refactor_cache", dict)
    monkeypatch.setattr(module, "save_refactor_cache", lambda cache: None)

    def fake_gather(misses, *, model, prompts, on_success=None):  # type: ignore[no-untyped-def]
        return {}, [
            (pending.unit_id, pending.helper_name, ConnectionError("llm down")),
        ]

    monkeypatch.setattr(module, "_gather_semantic_naming", fake_gather)

    with caplog.at_level(logging.WARNING, logger="src.internals_refactor"):
        index, prepared = module._run_semantic_naming_pass(
            (pending,),
            internals_path=internals_path,
            internals_index=module.InternalsSourceIndex.from_source(source),
            runtime_source=runtime_source,
            dry_run=False,
        )

    assert prepared == {}
    assert "Mechanically synthesized helper pending semantic naming." in index.source
    assert internals_path.read_text(encoding="utf-8") == source
    warning_text = "\n".join(caplog.messages)
    assert pending.helper_name in warning_text
    assert "run_semantic_naming" in warning_text
    assert str(internals_path) in warning_text or "--internals" in warning_text


def test_run_semantic_naming_pass_applies_successes_despite_sibling_gather_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One gather failure must not block applying another unit's naming."""
    import logging

    from src import internals_refactor as module
    from src.mechanical_body import MechanicalBodyDraft as Draft

    ctx_ok = _cluster_passthrough_context()
    draft_ok = _cluster_mechanical_draft()
    runtime_source = "def xl_cell(ctx, address): ...\n"
    mechanical_ok = build_mechanical_cluster_response(
        ctx_ok,
        draft_ok,
        runtime_source=runtime_source,
        internals_source="def cell_engine_c6(ctx):\n    return 1\n",
    )
    # Second helper: same shape, distinct name, marked pending.
    helper_bad = "other_passthrough"
    bad_body = draft_ok.body
    bad_doc = mechanical_placeholder_docstring(["time_period"])
    bad_helper_source = (
        f"def {helper_bad}(ctx, *, time_period):\n"
        f'    """{bad_doc}"""\n'
        + "\n".join(f"    {line}" for line in bad_body.splitlines())
        + "\n"
    )
    source = (
        "from __future__ import annotations\n\n"
        + mechanical_ok.helper_source.strip()
        + "\n\n"
        + bad_helper_source
        + "\n"
        + "def cell_engine_c6(ctx):\n"
        + "    return combined_input_passthrough(ctx, time_period=1)\n\n"
        + "def cell_engine_d6(ctx):\n"
        + "    return combined_input_passthrough(ctx, time_period=2)\n"
    )
    pending_ok = module._PendingSemanticUnit(
        kind="cluster",
        unit_id="cluster:ok",
        helper_name=mechanical_ok.helper_name,
        diagnostic_target="cluster:ok",
        canonical_template=ctx_ok.canonical_template,
        contract=ctx_ok.contract,
        draft=draft_ok,
        ctx=ctx_ok,
        parameter_names=frozenset(p.name for p in mechanical_ok.parameters),
        forbidden_names=frozenset(
            {
                mechanical_ok.helper_name,
                helper_bad,
                "xl_cell",
                "cell_engine_c6",
                "cell_engine_d6",
            }
        ),
        member_fingerprints=(),
        member_checks=(),
    )
    # Minimal ctx for the failing sibling — gather fails before apply uses it.
    pending_bad = module._PendingSemanticUnit(
        kind="cluster",
        unit_id="cluster:bad",
        helper_name=helper_bad,
        diagnostic_target="cluster:bad",
        canonical_template=ctx_ok.canonical_template,
        contract=ctx_ok.contract,
        draft=Draft(
            body=bad_body,
            renameable_locals=draft_ok.renameable_locals,
            lookup_table_names=(),
            group_count=1,
        ),
        ctx=ctx_ok,
        parameter_names=frozenset({"time_period"}),
        forbidden_names=pending_ok.forbidden_names,
        member_fingerprints=(),
        member_checks=(),
    )
    internals_path = tmp_path / "internals.py"
    internals_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(module, "REFACTOR_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(module, "load_refactor_cache", dict)
    monkeypatch.setattr(module, "save_refactor_cache", lambda cache: None)

    def fake_gather(misses, *, model, prompts, on_success=None):  # type: ignore[no-untyped-def]
        naming = _cluster_naming_response()
        if on_success is not None:
            on_success(pending_ok.unit_id, naming)
        return {pending_ok.unit_id: naming}, [
            (pending_bad.unit_id, pending_bad.helper_name, RuntimeError("bad unit")),
        ]

    monkeypatch.setattr(module, "_gather_semantic_naming", fake_gather)

    with caplog.at_level(logging.WARNING, logger="src.internals_refactor"):
        index, prepared = module._run_semantic_naming_pass(
            (pending_ok, pending_bad),
            internals_path=internals_path,
            internals_index=module.InternalsSourceIndex.from_source(source),
            runtime_source=runtime_source,
            dry_run=False,
        )

    assert pending_ok.unit_id in prepared
    assert pending_bad.unit_id not in prepared
    assert "columns_by_period" in index.source
    assert (
        "Mechanically synthesized helper pending semantic naming."
        in extract_function_source(index.source, helper_bad)
    )
    assert "pending semantic naming" not in extract_function_source(
        index.source, mechanical_ok.helper_name
    )
    warning_text = "\n".join(caplog.messages)
    assert helper_bad in warning_text
    assert "run_semantic_naming" in warning_text
