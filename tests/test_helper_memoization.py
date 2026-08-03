"""Tests for parameterized helper memoization polyfill and runtime patching."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.helper_memoization import (
    apply_xl_memoize_decorator,
    ensure_runtime_source_helper_memoization,
    make_xl_helper,
    make_xl_memoize,
    runtime_source_has_helper_memoization,
)
from src.pipeline_config import load_pipeline_config
from src.refactor_parity_gate import clear_parity_runtime_caches


@pytest.fixture
def helper_memo_dist_root(tmp_path: Path) -> Iterator[Path]:
    package_name = load_pipeline_config().dist_metadata.package_name
    package_root = tmp_path / package_name
    package_root.mkdir(parents=True)
    fixture_runtime = (
        Path(__file__).resolve().parent / "fixtures" / "parity_gate_runtime.py"
    )
    shutil.copy(fixture_runtime, package_root / "runtime.py")
    (package_root / "data.py").write_text(
        "DEFAULT_INPUTS = {}\nCONSTANTS = {}\n",
        encoding="utf-8",
    )
    clear_parity_runtime_caches()
    yield tmp_path
    clear_parity_runtime_caches()


def test_apply_xl_memoize_decorator_is_idempotent() -> None:
    source = (
        'def debt(ctx, time_period: int):\n    """Doc."""\n    return time_period\n'
    )
    once = apply_xl_memoize_decorator(source)
    assert once.startswith("@xl_memoize\n")
    assert apply_xl_memoize_decorator(once) == once


def test_ensure_runtime_source_injects_api(tmp_path: Path) -> None:
    stub = """\
from dataclasses import dataclass, field
from enum import StrEnum

class XlError(StrEnum):
    VALUE = "#VALUE!"

class XlErrorException(Exception):
    def __init__(self, code):
        self.code = code

@dataclass(slots=True)
class EvalContextBase:
    inputs: dict
    resolver: object
    cache: dict = field(default_factory=dict)
    computing: set = field(default_factory=set)
    iterative_enabled: bool = False
    iterate_count: int = 100
    iterate_delta: float = 0.001
    iteration_values: dict = field(default_factory=dict)

@dataclass(slots=True)
class EvalContext(EvalContextBase):
    deps: dict = field(default_factory=dict)
    reverse_deps: dict = field(default_factory=dict)
    stack: list = field(default_factory=list)

    def invalidate(self, addresses):
        for addr in addresses:
            self.cache.pop(addr, None)

    def set_inputs(self, inputs):
        self.inputs.update(inputs)
        self.invalidate(list(inputs))

def xl_circular_reference():
    return 0
"""
    assert not runtime_source_has_helper_memoization(stub)
    patched = ensure_runtime_source_helper_memoization(stub)
    assert runtime_source_has_helper_memoization(patched)
    assert "helper_cache:" in patched

    path = tmp_path / "runtime.py"
    path.write_text(patched, encoding="utf-8")
    namespace: dict[str, Any] = {}
    exec(compile(patched, str(path), "exec"), namespace)
    xl_memoize = namespace["xl_memoize"]
    EvalContext = namespace["EvalContext"]

    calls = {"n": 0}

    @xl_memoize
    def accum(ctx, *, time_period: int):
        calls["n"] += 1
        if time_period <= 1:
            return 1
        return accum(ctx, time_period=time_period - 1) + 1

    ctx = EvalContext(inputs={}, resolver=lambda _a: None)
    assert accum(ctx, time_period=20) == 20
    assert calls["n"] == 20
    assert accum(ctx, time_period=21) == 21
    assert calls["n"] == 21


def test_make_xl_helper_uses_runtime_error_types(helper_memo_dist_root: Path) -> None:
    from src.refactor_parity_gate import _runtime

    package_name = load_pipeline_config().dist_metadata.package_name
    runtime = _runtime(str((helper_memo_dist_root / package_name).resolve()))
    xl_helper = make_xl_helper(runtime)
    xl_memoize = make_xl_memoize(xl_helper)
    calls = {"n": 0}

    @xl_memoize
    def helper(ctx, *, time_period: int):
        calls["n"] += 1
        return time_period

    ctx = runtime.EvalContext(inputs={}, resolver=lambda _a: None)
    assert helper(ctx, time_period=3) == 3
    assert helper(ctx, time_period=3) == 3
    assert calls["n"] == 1
    assert xl_helper(ctx, helper.__wrapped__, time_period=3) == 3
    assert calls["n"] == 1
