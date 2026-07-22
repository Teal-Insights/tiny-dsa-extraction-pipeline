"""Standalone Pass-2 semantic naming from mechanical internals.py alone."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from src.mechanical_naming import ClusterNamingLLMResponse, LocalRename
from src.standalone_semantic_naming import (
    MECHANICAL_PENDING_DOCSTRING_SUMMARY,
    discover_pending_naming_helpers,
    run_standalone_semantic_naming,
)


def _pending_docstring(*, params: tuple[str, ...] = (), note: str) -> str:
    lines = [
        MECHANICAL_PENDING_DOCSTRING_SUMMARY,
        "",
        "Args:",
        "    ctx: Workbook evaluation context.",
    ]
    for name in params:
        lines.append(f"    {name}: Projection key parameter.")
    lines.extend(["", "Returns:", "    Cell value.", "", "Note:", f"    {note}"])
    return "\n".join(lines)


def _indent_docstring(docstring: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in docstring.splitlines())


def _mechanical_module() -> str:
    cluster_doc = _indent_docstring(
        _pending_docstring(
            params=("time_period",),
            note="Covers Engine!C6, Engine!D6. Excel: =Inputs!{col}1.",
        )
    )
    singleton_doc = _indent_docstring(
        _pending_docstring(note="Covers Engine!C9. Excel: =Inputs!C1*2.")
    )
    return (
        "from __future__ import annotations\n"
        "\n"
        "def already_named(ctx, time_period):\n"
        '    """Return a finished helper.\n'
        "\n"
        "    Args:\n"
        "        ctx: Workbook evaluation context.\n"
        "        time_period: Projection period.\n"
        "\n"
        "    Returns:\n"
        "        Cell value.\n"
        '    """\n'
        '    column = {1: "C", 2: "D"}[time_period]\n'
        '    return xl_cell(ctx, f"Inputs!{column}1")\n'
        "\n"
        "def passthrough_input(ctx, time_period):\n"
        '    """\n'
        f"{cluster_doc}\n"
        '    """\n'
        '    column_by_time_period = {1: "C", 2: "D"}\n'
        "    _t1 = column_by_time_period[time_period]\n"
        '    return xl_cell(ctx, f"Inputs!{_t1}1")\n'
        "\n"
        "def doubled_input(ctx):\n"
        '    """\n'
        f"{singleton_doc}\n"
        '    """\n'
        '    _t1 = xl_cell(ctx, "Inputs!C1")\n'
        "    _t2 = _t1 * 2\n"
        "    return _t2\n"
        "\n"
        "def cell_engine_c6(ctx):\n"
        "    return passthrough_input(ctx, time_period=1)\n"
    )


def test_discover_pending_helpers_from_mechanical_marker() -> None:
    helpers = discover_pending_naming_helpers(_mechanical_module())
    assert [helper.helper_name for helper in helpers] == [
        "passthrough_input",
        "doubled_input",
    ]
    cluster, singleton = helpers
    assert cluster.kind == "cluster"
    assert cluster.parameter_names == frozenset({"time_period"})
    assert cluster.draft.renameable_locals == ("_t1",)
    assert "column_by_time_period" in cluster.draft.lookup_table_names
    assert "_t1 = column_by_time_period[time_period]" in cluster.draft.body
    assert cluster.note_section.startswith("Covers Engine!C6")

    assert singleton.kind == "singleton"
    assert singleton.parameter_names == frozenset()
    assert singleton.draft.renameable_locals == ("_t1", "_t2")
    assert singleton.draft.lookup_table_names == ()


def test_discover_ignores_lambda_underscore_params() -> None:
    doc = _indent_docstring(_pending_docstring(note="Covers Sheet!A1. Excel: =1."))
    source = (
        "def helper(ctx):\n"
        '    """\n'
        f"{doc}\n"
        '    """\n'
        "    _t1 = sorted([1, 2], key=lambda _ln: _ln)\n"
        "    return _t1\n"
    )
    (helper,) = discover_pending_naming_helpers(source)
    assert helper.draft.renameable_locals == ("_t1",)
    assert "_ln" not in helper.draft.renameable_locals


def test_discover_multi_group_prefixed_temps() -> None:
    doc = _indent_docstring(
        _pending_docstring(
            params=("time_period",),
            note="Covers Engine!C6:D6. Excel: =A1.",
        )
    )
    source = (
        "def debt_path(ctx, time_period):\n"
        '    """\n'
        f"{doc}\n"
        '    """\n'
        "    if time_period == 1:\n"
        '        _f1_t1 = xl_cell(ctx, "Engine!C5")\n'
        "        return _f1_t1\n"
        "    _f2_t1 = debt_path(ctx, time_period=time_period - 1)\n"
        "    return _f2_t1\n"
    )
    (helper,) = discover_pending_naming_helpers(source)
    assert helper.draft.renameable_locals == ("_f1_t1", "_f2_t1")


def test_discover_fails_when_mechanical_temps_lack_pending_marker() -> None:
    source = dedent(
        '''
        def broken(ctx):
            """Some other docstring.

            Args:
                ctx: Context.

            Returns:
                Value.
            """
            _t1 = 1
            return _t1
        '''
    ).strip()
    with pytest.raises(ValueError, match="pending semantic naming"):
        discover_pending_naming_helpers(source)


def test_discover_tolerates_unreformed_cell_functions_with_temps() -> None:
    """Codegen cell_* bodies keep _tN locals; they are not pending helpers."""
    pending = _indent_docstring(
        _pending_docstring(note="Covers Engine!C6. Excel: =Inputs!C1.")
    )
    source = (
        "def passthrough_input(ctx):\n"
        '    """\n'
        f"{pending}\n"
        '    """\n'
        "    _t1 = xl_cell(ctx, 'Inputs!C1')\n"
        "    return _t1\n"
        "\n"
        "def cell_engine_c9(ctx):\n"
        '    """Formula: =Inputs!C1."""\n'
        "    _t1 = xl_cell(ctx, 'Inputs!C1')\n"
        "    return _t1\n"
    )
    helpers = discover_pending_naming_helpers(source)
    assert [helper.helper_name for helper in helpers] == ["passthrough_input"]


def test_run_standalone_renames_locals_and_replaces_docstring() -> None:
    source = _mechanical_module()
    responses = {
        "passthrough_input": ClusterNamingLLMResponse(
            symbol_docstring=(
                "Return the passthrough input for a projection period.\n\n"
                "Args:\n"
                "    ctx: Workbook evaluation context.\n"
                "    time_period: Projection period.\n\n"
                "Returns:\n"
                "    The corresponding input value."
            ),
            renames=(LocalRename(original="_t1", replacement="column"),),
            error=None,
            error_reason=None,
        ),
        "doubled_input": ClusterNamingLLMResponse(
            symbol_docstring=(
                "Double the Inputs!C1 value.\n\n"
                "Args:\n"
                "    ctx: Workbook evaluation context.\n\n"
                "Returns:\n"
                "    Doubled input."
            ),
            renames=(
                LocalRename(original="_t1", replacement="base_value"),
                LocalRename(original="_t2", replacement="doubled_value"),
            ),
            error=None,
            error_reason=None,
        ),
    }
    named = run_standalone_semantic_naming(
        source,
        naming_responses=responses,
        allowed_runtime_symbols=("xl_cell",),
        use_cache=False,
    )
    assert MECHANICAL_PENDING_DOCSTRING_SUMMARY not in named
    assert "column = column_by_time_period[time_period]" in named
    assert "base_value = xl_cell(ctx, 'Inputs!C1')" in named
    assert "doubled_value = base_value * 2" in named
    assert "_t1" not in named
    assert "Covers Engine!C6, Engine!D6" in named
    assert "Covers Engine!C9" in named
    assert "Return the passthrough input" in named


def test_run_standalone_rejects_incomplete_rename_map() -> None:
    source = _mechanical_module()
    responses = {
        "passthrough_input": ClusterNamingLLMResponse(
            symbol_docstring=(
                "Doc.\n\nArgs:\n    ctx: Context.\n\nReturns:\n    Value."
            ),
            renames=(),
            error=None,
            error_reason=None,
        ),
        "doubled_input": ClusterNamingLLMResponse(
            symbol_docstring=(
                "Doc.\n\nArgs:\n    ctx: Context.\n\nReturns:\n    Value."
            ),
            renames=(
                LocalRename(original="_t1", replacement="base_value"),
                LocalRename(original="_t2", replacement="doubled_value"),
            ),
            error=None,
            error_reason=None,
        ),
    }
    with pytest.raises(ValueError, match="missing: _t1"):
        run_standalone_semantic_naming(
            source,
            naming_responses=responses,
            allowed_runtime_symbols=("xl_cell",),
            use_cache=False,
        )


def test_run_standalone_writes_module_and_uses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import standalone_semantic_naming as naming_module

    internals_path = tmp_path / "internals.py"
    internals_path.write_text(_mechanical_module() + "\n", encoding="utf-8")
    cache: dict[str, str] = {}
    monkeypatch.setattr(naming_module, "load_refactor_cache", lambda: cache)
    monkeypatch.setattr(
        naming_module,
        "save_refactor_cache",
        lambda new_cache: cache.update(new_cache),
    )
    monkeypatch.setattr("src.internals_refactor.refactor_model", lambda: "test-model")

    responses = {
        "passthrough_input": ClusterNamingLLMResponse(
            symbol_docstring=(
                "Passthrough.\n\nArgs:\n    ctx: C.\n    time_period: P.\n\n"
                "Returns:\n    V."
            ),
            renames=(LocalRename(original="_t1", replacement="column"),),
            error=None,
            error_reason=None,
        ),
        "doubled_input": ClusterNamingLLMResponse(
            symbol_docstring=("Doubled.\n\nArgs:\n    ctx: C.\n\nReturns:\n    V."),
            renames=(
                LocalRename(original="_t1", replacement="base_value"),
                LocalRename(original="_t2", replacement="doubled_value"),
            ),
            error=None,
            error_reason=None,
        ),
    }
    named = run_standalone_semantic_naming(
        internals_path.read_text(encoding="utf-8"),
        naming_responses=responses,
        allowed_runtime_symbols=("xl_cell",),
        internals_path=internals_path,
        dry_run=False,
    )
    assert internals_path.read_text(encoding="utf-8") == named
    assert cache, "standalone naming should populate the refactor cache"


def test_run_standalone_requires_no_projection_or_graph() -> None:
    """Happy path depends only on the mechanical module (+ runtime allowlist)."""
    doc = _indent_docstring(_pending_docstring(note="Covers A!B1. Excel: =1."))
    source = f'def helper(ctx):\n    """\n{doc}\n    """\n    _t1 = 1\n    return _t1\n'
    named = run_standalone_semantic_naming(
        source,
        naming_responses={
            "helper": ClusterNamingLLMResponse(
                symbol_docstring=(
                    "Constant one.\n\nArgs:\n    ctx: Context.\n\nReturns:\n    1."
                ),
                renames=(LocalRename(original="_t1", replacement="one"),),
                error=None,
                error_reason=None,
            )
        },
        allowed_runtime_symbols=(),
        use_cache=False,
    )
    assert "one = 1" in named


def test_run_standalone_persists_cache_as_each_gather_response_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-gather failures must not discard already-completed naming responses."""
    from src import standalone_semantic_naming as naming_module

    cache: dict[str, str] = {}
    save_calls: list[int] = []

    monkeypatch.setattr(naming_module, "load_refactor_cache", lambda: dict(cache))

    def _save(new_cache: dict[str, str]) -> None:
        cache.clear()
        cache.update(new_cache)
        save_calls.append(len(cache))

    monkeypatch.setattr(naming_module, "save_refactor_cache", _save)
    monkeypatch.setattr("src.internals_refactor.refactor_model", lambda: "test-model")

    source = _mechanical_module()
    responses = {
        "passthrough_input": ClusterNamingLLMResponse(
            symbol_docstring=(
                "Passthrough.\n\nArgs:\n    ctx: C.\n    time_period: P.\n\n"
                "Returns:\n    V."
            ),
            renames=(LocalRename(original="_t1", replacement="column"),),
            error=None,
            error_reason=None,
        ),
        "doubled_input": ClusterNamingLLMResponse(
            symbol_docstring=("Doubled.\n\nArgs:\n    ctx: C.\n\nReturns:\n    V."),
            renames=(
                LocalRename(original="_t1", replacement="base_value"),
                LocalRename(original="_t2", replacement="doubled_value"),
            ),
            error=None,
            error_reason=None,
        ),
    }

    def _gather_then_fail(misses, prompts, *, on_success=None):
        del prompts
        names = {helper.helper_name for helper in misses}
        assert names == {"passthrough_input", "doubled_input"}
        first = misses[0]
        assert on_success is not None, "gather must receive on_success for resumability"
        on_success(first.helper_name, responses[first.helper_name])
        raise ConnectionError("simulated mid-gather network failure")

    with pytest.raises(ConnectionError, match="simulated mid-gather"):
        run_standalone_semantic_naming(
            source,
            gather_responses=_gather_then_fail,
            allowed_runtime_symbols=("xl_cell",),
            internals_path=tmp_path / "internals.py",
            dry_run=False,
            use_cache=True,
        )

    assert len(cache) == 1, "first successful response must be on disk before failure"
    assert save_calls == [1]
    cached_helper = next(
        name
        for name, response in responses.items()
        if any(response.model_dump_json() == payload for payload in cache.values())
    )

    call_counts: list[str] = []

    def _gather_remainder(misses, prompts, *, on_success=None):
        del prompts
        names = [helper.helper_name for helper in misses]
        call_counts.extend(names)
        assert len(names) == 1 and names[0] != cached_helper
        assert on_success is not None
        out: dict[str, ClusterNamingLLMResponse] = {}
        for helper in misses:
            response = responses[helper.helper_name]
            on_success(helper.helper_name, response)
            out[helper.helper_name] = response
        return out

    named = run_standalone_semantic_naming(
        source,
        gather_responses=_gather_remainder,
        allowed_runtime_symbols=("xl_cell",),
        internals_path=tmp_path / "internals.py",
        dry_run=False,
        use_cache=True,
    )
    assert len(call_counts) == 1
    assert call_counts[0] != cached_helper
    assert len(cache) == 2
    assert "column = column_by_time_period[time_period]" in named
    assert "doubled_value = base_value * 2" in named
