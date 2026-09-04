"""LLM docstring overlay for inverted-tree helpers (pipeline annotate stage)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.inverted_tree_docstrings import (
    ArgDoc,
    FunctionDocRequest,
    FunctionDocResponse,
    apply_inverted_tree_docstrings,
    function_doc_cache_key,
    llm_generate_docs,
    render_google_docstring,
    replace_function_docstring,
    save_docstring_cache,
)
from src.llm_providers import (
    LLM_MAX_CONCURRENT_ENV,
    get_llm_semaphore,
    reset_llm_semaphore,
)


@pytest.fixture(autouse=True)
def _reset_semaphore() -> Iterator[None]:
    reset_llm_semaphore()
    yield
    reset_llm_semaphore()


def _response_for(request: FunctionDocRequest) -> FunctionDocResponse:
    return FunctionDocResponse(
        summary=f"{request.function_name} summary.",
        purpose=f"{request.function_name} purpose.",
        args=tuple(
            ArgDoc(name=name, description=f"{name} arg.")
            for name in request.parameter_names
        ),
        returns="Result values.",
    )


def _request(
    module_name: str,
    function_name: str,
    parameter_names: tuple[str, ...] = ("country_name",),
) -> FunctionDocRequest:
    params = ", ".join(f"{name}: str" for name in parameter_names)
    return FunctionDocRequest(
        module_name=module_name,
        function_name=function_name,
        parameter_names=parameter_names,
        source=(
            f"def {function_name}({params}):\n"
            '    """Mechanical placeholder."""\n'
            "    return 1\n"
        ),
        series_notes="",
        guide_text="guide",
    )


def _mechanical_internals() -> str:
    return (
        "from __future__ import annotations\n"
        "\n"
        "def helper_series(year_labels: list[int], shock_year: int) -> tuple[int, ...]:\n"
        '    """First-level helper for bound series `helper_series`."""\n'
        "    return tuple(1 for _ in year_labels)\n"
    )


def test_replace_function_docstring_splices_google_block() -> None:
    source = _mechanical_internals()
    rendered = render_google_docstring(
        FunctionDocResponse(
            summary="Shock-year activation flags.",
            purpose="Return 1 when the year is at or after the shock year.",
            args=(
                ArgDoc(name="year_labels", description="Projection year labels."),
                ArgDoc(name="shock_year", description="First year the shock applies."),
            ),
            returns="One flag per projection year.",
        )
    )
    updated = replace_function_docstring(source, "helper_series", rendered)
    assert "First-level helper" not in updated
    assert "Shock-year activation flags." in updated
    assert "year_labels:" in updated
    assert "return tuple(1 for _ in year_labels)" in updated


def test_apply_inverted_tree_docstrings_uses_injected_generator(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "api.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "def compute_output_baseline(\n"
        "    *,\n"
        "    country_name: str,\n"
        "    growth_baseline: list[float],\n"
        ") -> tuple[float, ...]:\n"
        '    """Compute `output_baseline` from its subgraph leaf closure."""\n'
        "    return (1.0,)\n",
        encoding="utf-8",
    )
    (package / "internals.py").write_text(_mechanical_internals(), encoding="utf-8")

    calls: list[str] = []

    def generate(request) -> FunctionDocResponse:
        calls.append(request.function_name)
        names = request.parameter_names
        return FunctionDocResponse(
            summary=f"{request.function_name} summary.",
            purpose=f"{request.function_name} purpose.",
            args=tuple(ArgDoc(name=name, description=f"{name} arg.") for name in names),
            returns="Result values.",
        )

    apply_inverted_tree_docstrings(
        package_root=package,
        series_notes={},
        guide_text="Guide text.",
        generate_doc=generate,
    )

    assert "helper_series" in calls
    assert "compute_output_baseline" in calls
    internals = (package / "internals.py").read_text(encoding="utf-8")
    api = (package / "api.py").read_text(encoding="utf-8")
    assert "helper_series summary." in internals
    assert "compute_output_baseline summary." in api
    namespace: dict[str, object] = {}
    exec(compile(internals, str(package / "internals.py"), "exec"), namespace)  # noqa: S102
    helper = namespace["helper_series"]
    assert callable(helper)
    doc = inspect.getdoc(helper)
    assert doc is not None
    assert "year_labels arg." in doc


def test_apply_inverted_tree_docstrings_rejects_arg_mismatch(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "api.py").write_text(
        "def compute_output_baseline(*, country_name: str) -> tuple[float, ...]:\n"
        '    """Compute `output_baseline` from its subgraph leaf closure."""\n'
        "    return (1.0,)\n",
        encoding="utf-8",
    )
    (package / "internals.py").write_text("# empty\n", encoding="utf-8")

    def generate(request) -> FunctionDocResponse:
        return FunctionDocResponse(
            summary="Wrong.",
            purpose="Wrong args.",
            args=(ArgDoc(name="not_a_param", description="nope"),),
            returns="nope",
        )

    try:
        apply_inverted_tree_docstrings(
            package_root=package,
            series_notes={},
            guide_text="guide",
            generate_doc=generate,
        )
    except ValueError as error:
        assert "compute_output_baseline" in str(error)
        assert "not_a_param" in str(error)
    else:
        raise AssertionError("expected ValueError for arg mismatch")


def _patch_async_doc_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    requests: tuple[FunctionDocRequest, ...],
    delay: float = 0.0,
) -> dict[str, int]:
    """Replace the async LLM call with a delayed in-process stub.

    Tracks how many requests are in flight so tests can assert fan-out.
    """
    monkeypatch.setenv("DOCSTRING_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    by_prompt = {request.function_name: _response_for(request) for request in requests}
    stats = {"in_flight": 0, "max_in_flight": 0, "calls": 0}

    async def fake_generate_validated_json_async(
        **kwargs: object,
    ) -> tuple[FunctionDocResponse, str]:
        user_prompt = kwargs["user_prompt"]
        assert isinstance(user_prompt, str)
        match = next(
            response
            for name, response in by_prompt.items()
            if f".{name}" in user_prompt or f"Function: {name}" in user_prompt
        )
        limiter = kwargs.get("semaphore")
        if limiter is None:
            limiter = get_llm_semaphore()
        assert isinstance(limiter, asyncio.Semaphore)
        async with limiter:
            stats["in_flight"] += 1
            stats["max_in_flight"] = max(stats["max_in_flight"], stats["in_flight"])
            stats["calls"] += 1
            try:
                if delay:
                    await asyncio.sleep(delay)
                return match, match.model_dump_json()
            finally:
                stats["in_flight"] -= 1

    monkeypatch.setattr(
        "src.inverted_tree_docstrings.generate_validated_json_async",
        fake_generate_validated_json_async,
    )
    monkeypatch.setattr(
        "src.inverted_tree_docstrings.build_async_client",
        lambda model, **kwargs: (object(), object()),
    )
    return stats


def test_apply_inverted_tree_docstrings_batches_llm_for_all_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "api.py").write_text(
        "def compute_output_baseline(*, country_name: str) -> tuple[float, ...]:\n"
        '    """Compute `output_baseline` from its subgraph leaf closure."""\n'
        "    return (1.0,)\n",
        encoding="utf-8",
    )
    (package / "internals.py").write_text(_mechanical_internals(), encoding="utf-8")

    batched: list[int] = []

    def fake_llm_generate_docs(
        requests: tuple[FunctionDocRequest, ...] | list[FunctionDocRequest],
        **kwargs: object,
    ) -> dict[tuple[str, str], FunctionDocResponse]:
        batched.append(len(tuple(requests)))
        return {
            (request.module_name, request.function_name): _response_for(request)
            for request in requests
        }

    monkeypatch.setattr(
        "src.inverted_tree_docstrings.llm_generate_docs",
        fake_llm_generate_docs,
    )

    apply_inverted_tree_docstrings(
        package_root=package,
        series_notes={},
        guide_text="guide",
        repo_root=tmp_path,
    )

    assert batched == [2]
    api = (package / "api.py").read_text(encoding="utf-8")
    internals = (package / "internals.py").read_text(encoding="utf-8")
    assert "compute_output_baseline summary." in api
    assert "helper_series summary." in internals


def test_llm_generate_docs_fans_out_under_semaphore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_MAX_CONCURRENT_ENV, "2")
    reset_llm_semaphore()
    requests = (
        _request("api.py", "alpha"),
        _request("api.py", "bravo"),
        _request("internals.py", "charlie"),
    )
    stats = _patch_async_doc_llm(monkeypatch, requests=requests, delay=0.05)

    results = llm_generate_docs(
        requests,
        repo_root=tmp_path,
        cache_path=tmp_path / "docs.json",
        no_cache=True,
    )

    assert stats["calls"] == 3
    assert stats["max_in_flight"] > 1
    assert stats["max_in_flight"] <= 2
    assert set(results) == {
        ("api.py", "alpha"),
        ("api.py", "bravo"),
        ("internals.py", "charlie"),
    }


def test_llm_generate_docs_skips_cached_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = _request("api.py", "alpha")
    miss = _request("api.py", "bravo")
    cache_path = tmp_path / "docs.json"
    monkeypatch.setenv("DOCSTRING_MODEL", "deepseek-v4-flash")
    save_docstring_cache(
        cache_path,
        {
            function_doc_cache_key(cached, model="deepseek-v4-flash"): _response_for(
                cached
            ).model_dump_json()
        },
    )
    stats = _patch_async_doc_llm(monkeypatch, requests=(cached, miss), delay=0.0)

    results = llm_generate_docs(
        (cached, miss),
        repo_root=tmp_path,
        cache_path=cache_path,
    )

    assert stats["calls"] == 1
    assert results[("api.py", "alpha")].summary == "alpha summary."
    assert results[("api.py", "bravo")].summary == "bravo summary."
