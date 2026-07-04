"""Reset shared pipeline module state between tests."""

from __future__ import annotations

import sys

_PROBE_MODULE_NAMES = (
    "_runtime_symbols_probe",
    "_exported_runtime_for_parity",
    "_exported_data_for_parity",
    "tests.differential.differential_test_exported_library",
)


def clear_runtime_caches() -> None:
    from src.runtime_symbols import allowed_runtime_symbols

    allowed_runtime_symbols.cache_clear()
    try:
        from src.refactor_parity_gate import _dist_data, _runtime

        _runtime.cache_clear()
        _dist_data.cache_clear()
    except ImportError:
        pass


def reset_pipeline_test_state() -> None:
    from src.pipeline_context import reset_pipeline_config

    reset_pipeline_config()
    clear_runtime_caches()
    for module_name in _PROBE_MODULE_NAMES:
        sys.modules.pop(module_name, None)
