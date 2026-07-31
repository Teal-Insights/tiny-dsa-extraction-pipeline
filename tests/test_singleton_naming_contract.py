"""Tests for the naming-only singleton contract (issue #45, phase 3)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from textwrap import dedent
from typing import cast

import pytest

from src.internals_refactor import (
    InternalsSourceIndex,
    SingletonRefactorContext,
    SingletonRefactorLLMResponse,
    _prepare_singleton_refactor_response,
    _singleton_inline_replacements,
    _try_synthesize_singleton_body,
    prepare_singleton_refactor_response,
    validate_singleton_refactor_response,
)
from src.mechanical_naming import (
    LocalRename,
    SingletonNamingLLMResponse,
    apply_cluster_naming_response,
    load_singleton_naming_prompt_fixed_portion,
)

ALLOWED_RUNTIME_SYMBOLS = ("XlError", "xl_cell", "xl_eval", "xl_number")

INTERNALS_WITH_THIN_WRAPPERS = dedent(
    '''
    from __future__ import annotations

    from .runtime import xl_cell, xl_number

    def shock_active(ctx, time_period):
        """Shock active flag.

        Args:
            ctx: Workbook evaluation context.
            time_period: Projection year.

        Returns:
            1.0 when active.
        """
        return 1.0

    def cell_engine_c10(ctx):
        return shock_active(ctx, time_period=1)

    def cell_inputs_b5(ctx):
        return xl_cell(ctx, 'Inputs!B5')

    def cell_outputs_b12(ctx):
        _t1 = cell_engine_c10(ctx)
        _t2 = cell_inputs_b5(ctx)
        return xl_number(_t1) * xl_number(_t2)
    '''
).strip()


def _context() -> SingletonRefactorContext:
    index = InternalsSourceIndex.from_source(INTERNALS_WITH_THIN_WRAPPERS)
    python_source = index.function_source("cell_outputs_b12")
    dependency_addresses = ("Engine!C10", "Inputs!B5")
    return SingletonRefactorContext(
        address="Outputs!B12",
        function_name="cell_outputs_b12",
        canonical_template="=1",
        normalized_formula="=Engine!C10*Inputs!B5",
        python_source=python_source,
        dependency_addresses=dependency_addresses,
        external_dependencies=("shock_active",),
        semantic_dependencies=(),
        call_sites=(),
        allowed_runtime_symbols=ALLOWED_RUNTIME_SYMBOLS,
        naming_hints={},
        expected_helper_name="shocked_product",
        inline_replacements=_singleton_inline_replacements(
            python_source, dependency_addresses, index
        ),
    )


def test_inline_replacements_cover_only_recorded_dependency_wrappers() -> None:
    ctx = _context()
    assert dict(ctx.inline_replacements) == {
        "cell_engine_c10": "shock_active(ctx, time_period=1)",
        "cell_inputs_b5": "xl_cell(ctx, 'Inputs!B5')",
    }

    index = InternalsSourceIndex.from_source(INTERNALS_WITH_THIN_WRAPPERS)
    without_dependency = _singleton_inline_replacements(
        ctx.python_source, ("Engine!C10",), index
    )
    assert dict(without_dependency) == {
        "cell_engine_c10": "shock_active(ctx, time_period=1)"
    }


def test_try_synthesize_singleton_body_assembles_draft() -> None:
    draft = _try_synthesize_singleton_body(_context())
    assert draft is not None
    assert draft.body == (
        "_t1 = shock_active(ctx, time_period=1)\n"
        "_t2 = xl_cell(ctx, 'Inputs!B5')\n"
        "return xl_number(_t1) * xl_number(_t2)"
    )
    assert draft.renameable_locals == ("_t1", "_t2")


def test_try_synthesize_singleton_body_respects_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MECHANICAL_REFACTOR_BODIES", "0")
    assert _try_synthesize_singleton_body(_context()) is None


def test_try_synthesize_singleton_body_falls_back_on_uninlinable_dependency() -> None:
    without_replacements = replace(_context(), inline_replacements=())
    assert _try_synthesize_singleton_body(without_replacements) is None


def test_naming_response_round_trips_through_singleton_validation() -> None:
    ctx = _context()
    draft = _try_synthesize_singleton_body(ctx)
    assert draft is not None
    naming_response = SingletonNamingLLMResponse(
        symbol_docstring=(
            "Product of the shock flag and the raw input.\n\n"
            "Args:\n    ctx: Workbook evaluation context.\n\n"
            "Returns:\n    Shocked product."
        ),
        renames=(
            LocalRename(original="_t1", replacement="shock_flag"),
            LocalRename(original="_t2", replacement="raw_input_value"),
        ),
        error=None,
        error_reason=None,
    )
    body = apply_cluster_naming_response(
        naming_response,
        draft,
        parameter_names=frozenset(),
        forbidden_names=frozenset({"shock_active", "xl_cell", "xl_number"}),
    )
    assert body == (
        "shock_flag = shock_active(ctx, time_period=1)\n"
        "raw_input_value = xl_cell(ctx, 'Inputs!B5')\n"
        "return xl_number(shock_flag) * xl_number(raw_input_value)"
    )

    prepared = _prepare_singleton_refactor_response(
        prepare_singleton_refactor_response(
            SingletonRefactorLLMResponse(
                symbol_docstring=naming_response.symbol_docstring,
                symbol_body=body,
                error=None,
                error_reason=None,
            ),
            ctx,
            runtime_source=(
                "def xl_number(value: CellValue) -> float:\n"
                '    """Coerce a scalar cell value to a number."""\n'
                "    ...\n"
            ),
            internals_source=INTERNALS_WITH_THIN_WRAPPERS,
        ),
        ctx,
    )
    validate_singleton_refactor_response(
        ctx,
        prepared,
        existing_names=frozenset(
            {
                "shock_active",
                "cell_engine_c10",
                "cell_inputs_b5",
                "cell_outputs_b12",
            }
        ),
        internals_source=INTERNALS_WITH_THIN_WRAPPERS,
    )
    assert "shock_flag = shock_active(ctx, time_period=1)" in prepared.symbol_source


def test_llm_refactor_singleton_selects_naming_contract_when_synthesizable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import src.internals_refactor as module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(INTERNALS_WITH_THIN_WRAPPERS + "\n", encoding="utf-8")
    (tmp_path / "runtime.py").write_text(
        "def xl_cell(ctx, address):\n    return 0\n\n"
        "def xl_number(value) -> float:\n    return 0.0\n",
        encoding="utf-8",
    )
    ctx = _context()
    recorded: dict[str, object] = {}

    def fake_generate_validated_json(**kwargs: object):
        recorded["response_model"] = kwargs["response_model"]
        recorded["user_prompt"] = kwargs["user_prompt"]
        response = SingletonNamingLLMResponse(
            symbol_docstring=(
                "Product of the shock flag and the raw input.\n\n"
                "Args:\n    ctx: Workbook evaluation context.\n\n"
                "Returns:\n    Shocked product."
            ),
            renames=(
                LocalRename(original="_t1", replacement="shock_flag"),
                LocalRename(original="_t2", replacement="raw_input_value"),
            ),
            error=None,
            error_reason=None,
        )
        post_validate = cast(
            Callable[[SingletonNamingLLMResponse], SingletonNamingLLMResponse],
            kwargs["post_validate"],
        )
        validated = post_validate(response)
        return validated, response.model_dump_json()

    monkeypatch.setattr(module, "generate_validated_json", fake_generate_validated_json)
    monkeypatch.setattr(module, "load_refactor_cache", dict)
    monkeypatch.setattr(module, "save_refactor_cache", lambda _cache: None)
    monkeypatch.setattr(module, "refactor_model", lambda: "test-model")
    monkeypatch.setattr(module, "build_client", lambda _model: (object(), object()))

    response = module.llm_refactor_singleton(ctx, internals_path=internals_path)

    assert recorded["response_model"] is SingletonNamingLLMResponse
    user_prompt = recorded["user_prompt"]
    assert isinstance(user_prompt, str)
    assert "Mechanical draft body" in user_prompt
    assert "Renameable locals: _t1, _t2" in user_prompt
    assert response.symbol_name == "shocked_product"
    assert "shock_flag = shock_active(ctx, time_period=1)" in response.symbol_source
    assert "_t1" not in response.symbol_source


def test_singleton_naming_prompt_fixture_documents_contract() -> None:
    prompt = load_singleton_naming_prompt_fixed_portion()
    assert "SingletonNamingLLMResponse" in prompt
    assert "## Aborting" in prompt
    assert '"renames"' in prompt
    assert "helper_name" in prompt
    assert "Do not emit a function body" in prompt


def test_singleton_naming_response_error_escape_hatch() -> None:
    with pytest.raises(ValueError):
        SingletonNamingLLMResponse(
            symbol_docstring="Doc.",
            renames=(),
            error=True,
            error_reason="cannot document",
        )
    response = SingletonNamingLLMResponse(
        symbol_docstring=None,
        renames=None,
        error=True,
        error_reason="cannot document",
    )
    assert response.error is True
