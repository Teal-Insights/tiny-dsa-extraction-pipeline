"""Discover allowed runtime symbols from the exported runtime module."""

from __future__ import annotations

import ast
import sys
from functools import lru_cache
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType

# Sentinel-returning helpers retained for internal runtime use (coercion
# primitives and skip-semantics scans) but withheld from the generated-code
# allowlist. Generated formula code must use the raising ``xl_*`` wrappers
# (``xl_number``/``xl_int``/``xl_bool``/``xl_compare``) so Excel errors surface
# as exceptions rather than ``XlError`` sentinels.
_SENTINEL_RETURNING_EXCLUDED_SYMBOLS: frozenset[str] = frozenset(
    {"to_number", "to_int", "to_bool", "compare_scalars"}
)


def _load_runtime_module(path: Path) -> ModuleType:
    spec = importlib_util.spec_from_file_location("_runtime_symbols_probe", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"Could not load runtime module from {path}")
    module = importlib_util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _public_function_names(source: str) -> list[str]:
    tree = ast.parse(source)
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]


def discover_allowed_runtime_symbols(runtime_path: Path) -> tuple[str, ...]:
    """Return public formula-runtime callables and ``XlError`` from exported runtime."""
    source = runtime_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = _public_function_names(source)
    if any(
        isinstance(node, ast.ClassDef) and node.name == "XlError" for node in tree.body
    ):
        names.append("XlError")
    names = [name for name in names if name not in _SENTINEL_RETURNING_EXCLUDED_SYMBOLS]
    module = _load_runtime_module(runtime_path)
    try:
        for name in names:
            getattr(module, name)
        return tuple(sorted(names))
    finally:
        sys.modules.pop("_runtime_symbols_probe", None)


def discover_allowed_reader_symbols(readers_path: Path) -> tuple[str, ...]:
    """Return public ``read_*`` helpers from the exported ``_readers`` module.

    Missing ``_readers.py`` is treated as an empty set so older exports remain
    valid. Names are discovered via AST only: the module imports ``.runtime``
    relatively and is not executed in isolation here.
    """
    if not readers_path.is_file():
        return ()
    source = readers_path.read_text(encoding="utf-8")
    return tuple(sorted(_public_function_names(source)))


def discover_allowed_formula_symbols(
    runtime_path: Path,
    readers_path: Path | None = None,
) -> tuple[str, ...]:
    """Union runtime helpers with input-layer readers for refactor allowlists."""
    symbols = set(discover_allowed_runtime_symbols(runtime_path))
    if readers_path is not None:
        symbols.update(discover_allowed_reader_symbols(readers_path))
    return tuple(sorted(symbols))


@lru_cache(maxsize=8)
def _allowed_runtime_symbols_cached(package_root: str) -> tuple[str, ...]:
    root = Path(package_root)
    return discover_allowed_formula_symbols(root / "runtime.py", root / "_readers.py")


@lru_cache(maxsize=8)
def _allowed_runtime_module_symbols_cached(package_root: str) -> tuple[str, ...]:
    return discover_allowed_runtime_symbols(Path(package_root) / "runtime.py")


def allowed_runtime_symbols(package_root: Path) -> tuple[str, ...]:
    """Cached allowlist used by refactor validation and the parity gate."""
    return _allowed_runtime_symbols_cached(str(package_root.resolve()))


def allowed_runtime_module_symbols(package_root: Path) -> tuple[str, ...]:
    """Cached allowlist restricted to symbols exported by ``runtime.py``.

    :func:`allowed_runtime_symbols` unions in the ``_readers`` helpers, which
    generated modules import from ``._readers``. Import maintenance that
    rewrites the ``from .runtime import`` line needs this narrower set so
    reader helpers are never merged into the runtime import bundle.
    """
    return _allowed_runtime_module_symbols_cached(str(package_root.resolve()))


def clear_runtime_symbol_caches() -> None:
    """Drop path-keyed allowlist caches (e.g. after patching ``runtime.py``)."""
    _allowed_runtime_symbols_cached.cache_clear()
    _allowed_runtime_module_symbols_cached.cache_clear()
