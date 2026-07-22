"""Parameterized-helper memoization for warm ``EvalContext`` evaluation.

Address-keyed ``xl_cell`` / ``xl_eval`` caching does not cover direct helper
calls such as period recurrence ``f(t) → f(t-1)``. This module provides
``xl_helper`` / ``xl_memoize`` (matching the excel-grapher API) and helpers to
install them into an exported package ``runtime.py`` or a loaded runtime module
so mechanical refactor consumers and the batched parity gate share work under a
warm context.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
from collections.abc import Callable, Hashable, Iterable, Mapping, MutableMapping
from types import ModuleType
from typing import Any

# Fallback when EvalContext has no helper_cache field (older embedded runtimes).
_SIDE_HELPER_CACHES: dict[int, dict[tuple[Any, ...], Any]] = {}
_SIDE_HELPER_COMPUTING: dict[int, set[tuple[Any, ...]]] = {}

_RUNTIME_MARKER = "# --- parameterized helper memoization ---"


def clear_side_helper_memos() -> None:
    """Drop process-wide fallback memos (tests / end of a gate run)."""
    _SIDE_HELPER_CACHES.clear()
    _SIDE_HELPER_COMPUTING.clear()


def _freeze_helper_kwargs(
    kwargs: Mapping[str, object],
) -> tuple[tuple[str, Hashable], ...]:
    frozen: list[tuple[str, Hashable]] = []
    for name in sorted(kwargs):
        value = kwargs[name]
        if isinstance(value, Hashable):
            frozen.append((name, value))
            continue
        raise TypeError(
            f"xl_helper kwargs must be hashable for memoization; "
            f"got {name}={value!r} of type {type(value).__name__}"
        )
    return tuple(frozen)


def _helper_maps(
    ctx: Any,
) -> tuple[MutableMapping[tuple[Any, ...], Any], set[tuple[Any, ...]]]:
    helper_cache = getattr(ctx, "helper_cache", None)
    helper_computing = getattr(ctx, "helper_computing", None)
    if isinstance(helper_cache, dict) and isinstance(helper_computing, set):
        return helper_cache, helper_computing
    ctx_id = id(ctx)
    return (
        _SIDE_HELPER_CACHES.setdefault(ctx_id, {}),
        _SIDE_HELPER_COMPUTING.setdefault(ctx_id, set()),
    )


def make_xl_helper(runtime: ModuleType) -> Callable[..., Any]:
    """Build ``xl_helper`` bound to ``runtime``'s Excel-error types."""
    xl_error_exception = runtime.XlErrorException
    xl_circular_reference = runtime.xl_circular_reference

    def xl_helper(
        ctx: Any,
        fn: Callable[..., Any],
        /,
        **kwargs: object,
    ) -> Any:
        key = (fn, _freeze_helper_kwargs(kwargs))
        cache, computing = _helper_maps(ctx)
        if key in cache:
            value = cache[key]
            if isinstance(value, runtime.XlError):
                raise xl_error_exception(value)
            return value
        if key in computing:
            return xl_circular_reference()
        computing.add(key)
        try:
            try:
                value = fn(ctx, **kwargs)
            except xl_error_exception as exc:
                cache[key] = exc.code
                raise
            cache[key] = value
            if isinstance(value, runtime.XlError):
                raise xl_error_exception(value)
            return value
        finally:
            computing.discard(key)

    xl_helper.__name__ = "xl_helper"
    xl_helper.__qualname__ = "xl_helper"
    return xl_helper


def make_xl_memoize(xl_helper: Callable[..., Any]) -> Callable[..., Any]:
    """Build ``xl_memoize`` that routes decorated helpers through ``xl_helper``."""

    def xl_memoize(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(ctx: Any, /, *args: Any, **kwargs: Any) -> Any:
            if args:
                bound = inspect.signature(fn).bind(ctx, *args, **kwargs)
                bound.apply_defaults()
                param_kwargs = {
                    name: value
                    for name, value in bound.arguments.items()
                    if name != "ctx"
                }
                return xl_helper(ctx, fn, **param_kwargs)
            return xl_helper(ctx, fn, **kwargs)

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    xl_memoize.__name__ = "xl_memoize"
    xl_memoize.__qualname__ = "xl_memoize"
    return xl_memoize


def _patch_invalidate_for_side_caches(runtime: ModuleType) -> None:
    """Clear fallback side tables when ``EvalContext.invalidate`` runs."""
    eval_context = getattr(runtime, "EvalContext", None)
    if eval_context is None:
        return
    original = getattr(eval_context, "invalidate", None)
    if original is None or getattr(original, "_helper_memo_patched", False):
        return

    def invalidate(self: Any, addresses: Iterable[str]) -> None:
        _SIDE_HELPER_CACHES.pop(id(self), None)
        _SIDE_HELPER_COMPUTING.pop(id(self), None)
        helper_cache = getattr(self, "helper_cache", None)
        helper_computing = getattr(self, "helper_computing", None)
        if isinstance(helper_cache, dict):
            helper_cache.clear()
        if isinstance(helper_computing, set):
            helper_computing.clear()
        return original(self, addresses)

    setattr(invalidate, "_helper_memo_patched", True)
    eval_context.invalidate = invalidate


def install_helper_memoization(runtime: ModuleType) -> None:
    """Ensure ``runtime`` exposes ``xl_helper`` / ``xl_memoize`` and clears on invalidate."""
    if not hasattr(runtime, "xl_helper"):
        runtime.xl_helper = make_xl_helper(runtime)
    if not hasattr(runtime, "xl_memoize"):
        runtime.xl_memoize = make_xl_memoize(runtime.xl_helper)
    _patch_invalidate_for_side_caches(runtime)


def memoize_namespace_helpers(
    namespace: MutableMapping[str, Any],
    helper_names: Iterable[str],
    *,
    runtime: ModuleType,
) -> None:
    """Replace named helpers with ``xl_memoize`` wrappers (call-time name lookup)."""
    install_helper_memoization(runtime)
    xl_memoize = runtime.xl_memoize
    for name in helper_names:
        fn = namespace.get(name)
        if callable(fn) and getattr(fn, "__wrapped__", None) is None:
            # Avoid double-wrapping already-decorated helpers.
            if getattr(fn, "__xl_memoized__", False):
                continue
            wrapped = xl_memoize(fn)
            wrapped.__xl_memoized__ = True  # type: ignore[attr-defined]
            namespace[name] = wrapped


def apply_xl_memoize_decorator(helper_source: str) -> str:
    """Prefix a single-function helper source with ``@xl_memoize`` when missing."""
    module = ast.parse(helper_source)
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1:
        return helper_source
    function = functions[0]
    for decorator in function.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "xl_memoize":
            return helper_source
    # Preserve original formatting; only insert the decorator line.
    stripped = helper_source.lstrip()
    prefix = helper_source[: len(helper_source) - len(stripped)]
    return f"{prefix}@xl_memoize\n{stripped}"


def runtime_source_has_helper_memoization(source: str) -> bool:
    return "def xl_helper(" in source and "def xl_memoize(" in source


def _inject_helper_cache_fields(source: str) -> str:
    """Add ``helper_cache`` / ``helper_computing`` fields to ``EvalContextBase``."""
    if "helper_cache:" in source:
        return source
    match = re.search(
        r"^([ \t]*)iteration_values:\s*[^=\n]+=\s*field\(default_factory=dict\)\s*$",
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        return source
    indent = match.group(1)
    addition = (
        f"{match.group(0)}\n"
        f"{indent}helper_cache: dict = field(default_factory=dict)\n"
        f"{indent}helper_computing: set = field(default_factory=set)"
    )
    return source[: match.start()] + addition + source[match.end() :]


def _polyfill_source() -> str:
    return f'''
{_RUNTIME_MARKER}

def _xl_freeze_helper_kwargs(kwargs):
    frozen = []
    for name in sorted(kwargs):
        value = kwargs[name]
        try:
            hash(value)
        except TypeError as error:
            raise TypeError(
                f"xl_helper kwargs must be hashable for memoization; "
                f"got {{name}}={{value!r}} of type {{type(value).__name__}}"
            ) from error
        frozen.append((name, value))
    return tuple(frozen)


_XL_SIDE_HELPER_CACHES = {{}}
_XL_SIDE_HELPER_COMPUTING = {{}}


def _xl_helper_maps(ctx):
    helper_cache = getattr(ctx, "helper_cache", None)
    helper_computing = getattr(ctx, "helper_computing", None)
    if isinstance(helper_cache, dict) and isinstance(helper_computing, set):
        return helper_cache, helper_computing
    ctx_id = id(ctx)
    return (
        _XL_SIDE_HELPER_CACHES.setdefault(ctx_id, {{}}),
        _XL_SIDE_HELPER_COMPUTING.setdefault(ctx_id, set()),
    )


def xl_helper(ctx, fn, /, **kwargs):
    """Evaluate a parameterized helper, memoized by ``(fn, kwargs)`` on ``ctx``."""
    key = (fn, _xl_freeze_helper_kwargs(kwargs))
    cache, computing = _xl_helper_maps(ctx)
    if key in cache:
        value = cache[key]
        if isinstance(value, XlError):
            raise XlErrorException(value)
        return value
    if key in computing:
        return xl_circular_reference()
    computing.add(key)
    try:
        try:
            value = fn(ctx, **kwargs)
        except XlErrorException as exc:
            cache[key] = exc.code
            raise
        cache[key] = value
        if isinstance(value, XlError):
            raise XlErrorException(value)
        return value
    finally:
        computing.discard(key)


def xl_memoize(fn):
    """Decorator routing a ``(ctx, **params)`` helper through :func:`xl_helper`."""
    import functools as _functools
    import inspect as _inspect

    @_functools.wraps(fn)
    def wrapper(ctx, /, *args, **kwargs):
        if args:
            bound = _inspect.signature(fn).bind(ctx, *args, **kwargs)
            bound.apply_defaults()
            param_kwargs = {{
                name: value
                for name, value in bound.arguments.items()
                if name != "ctx"
            }}
            return xl_helper(ctx, fn, **param_kwargs)
        return xl_helper(ctx, fn, **kwargs)

    wrapper.__wrapped__ = fn
    return wrapper


def _xl_patch_eval_context_invalidate():
    original = EvalContext.invalidate

    def invalidate(self, addresses):
        _XL_SIDE_HELPER_CACHES.pop(id(self), None)
        _XL_SIDE_HELPER_COMPUTING.pop(id(self), None)
        helper_cache = getattr(self, "helper_cache", None)
        helper_computing = getattr(self, "helper_computing", None)
        if isinstance(helper_cache, dict):
            helper_cache.clear()
        if isinstance(helper_computing, set):
            helper_computing.clear()
        return original(self, addresses)

    EvalContext.invalidate = invalidate


_xl_patch_eval_context_invalidate()
'''


def ensure_runtime_source_helper_memoization(source: str) -> str:
    """Return runtime source with helper memoization API available."""
    updated = _inject_helper_cache_fields(source)
    if runtime_source_has_helper_memoization(updated):
        return updated
    return updated.rstrip() + "\n" + _polyfill_source()


def ensure_package_runtime_helper_memoization(runtime_path: Any) -> bool:
    """Patch ``runtime.py`` on disk when the helper memoization API is missing.

    Returns ``True`` when the file was rewritten.
    """
    from pathlib import Path

    path = Path(runtime_path)
    original = path.read_text(encoding="utf-8")
    updated = ensure_runtime_source_helper_memoization(original)
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True
