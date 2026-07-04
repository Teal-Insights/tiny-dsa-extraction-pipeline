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


def discover_allowed_runtime_symbols(runtime_path: Path) -> tuple[str, ...]:
    """Return public formula-runtime callables and ``XlError`` from exported runtime."""
    source = runtime_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name == "XlError":
            names.append(node.name)
    names = [name for name in names if name not in _SENTINEL_RETURNING_EXCLUDED_SYMBOLS]
    module = _load_runtime_module(runtime_path)
    try:
        for name in names:
            getattr(module, name)
        return tuple(sorted(names))
    finally:
        sys.modules.pop("_runtime_symbols_probe", None)


def _runtime_path_from_config() -> Path:
    from src.pipeline_context import require_pipeline_config

    return require_pipeline_config().package_root / "runtime.py"


@lru_cache(maxsize=1)
def allowed_runtime_symbols() -> tuple[str, ...]:
    """Cached allowlist used by refactor validation and the parity gate."""
    return discover_allowed_runtime_symbols(_runtime_path_from_config())
