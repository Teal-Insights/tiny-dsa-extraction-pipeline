from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pytest

from src.codegen_cache import (
    DEFAULT_CODEGEN_CACHE_DIR,
    clear_codegen_cache,
    codegen_cache_key,
    get_or_build_codegen_modules,
    load_codegen_payload,
    write_generated_modules,
)
from tests.fixtures.test_state import REPO_CODEGEN_CACHE_DIR

_SAMPLE_MODULES = {
    "__init__.py": "# init\n",
    "api.py": "def compute_example():\n    return (1.0,)\n",
    "data.py": "DATA = {}\n",
    "runtime.py": "def run():\n    pass\n",
    "internals.py": "def helper():\n    return 1.0\n",
}

_DOCSTRING_KEY_KWARGS = (
    "unpack_return",
    "docstring_renderer",
    "series_docstring_callback",
    "guide_sha256",
    "docstring_prompt_version",
    "docstring_model",
)


class _CodegenKeyKwargs(TypedDict):
    projection_cache_key: str
    targets: Sequence[str]
    paradigm: str


@pytest.fixture
def codegen_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "codegen"


def _key_kwargs(
    *,
    projection_cache_key: str = "proj-key-abc",
    targets: Sequence[str] = ("Sheet!A1",),
    paradigm: str = "inverted_tree",
) -> _CodegenKeyKwargs:
    return {
        "projection_cache_key": projection_cache_key,
        "targets": list(targets),
        "paradigm": paradigm,
    }


def test_pytest_uses_isolated_codegen_disk_cache() -> None:
    assert DEFAULT_CODEGEN_CACHE_DIR != REPO_CODEGEN_CACHE_DIR


def test_codegen_cache_key_requires_paradigm_and_omits_docstring_inputs() -> None:
    key_params = inspect.signature(codegen_cache_key).parameters
    build_params = inspect.signature(get_or_build_codegen_modules).parameters
    assert key_params["paradigm"].default is inspect.Parameter.empty
    assert build_params["paradigm"].default is inspect.Parameter.empty
    for name in _DOCSTRING_KEY_KWARGS:
        assert name not in key_params
        assert name not in build_params


def test_codegen_cache_roundtrip(codegen_cache_dir: Path) -> None:
    build = MagicMock(return_value=dict(_SAMPLE_MODULES))
    first = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )
    second = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )

    assert not first.cache_hit
    assert second.cache_hit
    assert first.cache_key == second.cache_key
    assert first.modules == second.modules == _SAMPLE_MODULES
    assert build.call_count == 1


def test_codegen_cache_force_rebuild(codegen_cache_dir: Path) -> None:
    build = MagicMock(return_value=dict(_SAMPLE_MODULES))
    first = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )
    second = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
        force_rebuild=True,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key == second.cache_key
    assert build.call_count == 2


def test_codegen_cache_no_cache_bypasses_disk(codegen_cache_dir: Path) -> None:
    build = MagicMock(return_value=dict(_SAMPLE_MODULES))
    result = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
        no_cache=True,
    )

    assert not result.cache_hit
    assert load_codegen_payload(result.cache_key, cache_dir=codegen_cache_dir) is None
    assert build.call_count == 1


def test_codegen_cache_key_follows_projection_cache_key() -> None:
    first = codegen_cache_key(**_key_kwargs(projection_cache_key="aaa"))
    second = codegen_cache_key(**_key_kwargs(projection_cache_key="bbb"))
    assert first != second


def test_codegen_cache_key_changes_with_targets_and_paradigm() -> None:
    base = codegen_cache_key(**_key_kwargs())
    assert base != codegen_cache_key(**_key_kwargs(targets=["Other!B2"]))
    assert base != codegen_cache_key(**_key_kwargs(paradigm="ctx"))


def test_codegen_cache_miss_when_projection_key_changes(
    codegen_cache_dir: Path,
) -> None:
    build = MagicMock(return_value=dict(_SAMPLE_MODULES))
    first = get_or_build_codegen_modules(
        **_key_kwargs(projection_cache_key="proj-a"),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )
    second = get_or_build_codegen_modules(
        **_key_kwargs(projection_cache_key="proj-b"),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )

    assert not first.cache_hit
    assert not second.cache_hit
    assert first.cache_key != second.cache_key
    assert build.call_count == 2


def test_corrupt_codegen_cache_is_rebuilt(codegen_cache_dir: Path) -> None:
    build = MagicMock(return_value=dict(_SAMPLE_MODULES))
    first = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )
    payload_path = codegen_cache_dir / f"{first.cache_key}.pkl.gz"
    payload_path.write_bytes(b"not-a-valid-gzip-pickle")

    second = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )
    assert not second.cache_hit
    assert payload_path.is_file()
    assert build.call_count == 2


def test_clear_codegen_cache_removes_entries(codegen_cache_dir: Path) -> None:
    build = MagicMock(return_value=dict(_SAMPLE_MODULES))
    result = get_or_build_codegen_modules(
        **_key_kwargs(),
        build_modules=build,
        cache_dir=codegen_cache_dir,
    )
    payload_path = codegen_cache_dir / f"{result.cache_key}.pkl.gz"
    assert payload_path.is_file()
    clear_codegen_cache(cache_dir=codegen_cache_dir)
    assert not payload_path.is_file()


def test_write_generated_modules_removes_stale_optional(tmp_path: Path) -> None:
    package_root = tmp_path / "pkg"
    package_root.mkdir()
    (package_root / "_readers.py").write_text("stale\n", encoding="utf-8")
    (package_root / "_api_helpers.py").write_text("stale\n", encoding="utf-8")
    (package_root / "_output_leaves.py").write_text("stale\n", encoding="utf-8")

    write_generated_modules(package_root, _SAMPLE_MODULES)

    assert (package_root / "api.py").read_text(encoding="utf-8") == _SAMPLE_MODULES[
        "api.py"
    ]
    assert not (package_root / "_readers.py").is_file()
    assert not (package_root / "_api_helpers.py").is_file()
    assert not (package_root / "_output_leaves.py").is_file()


def test_run_export_stage_skips_generate_modules_on_cache_hit(
    tmp_path: Path,
) -> None:
    from src import codegen_cache
    from src.extraction_pipeline import run_export_stage
    from tests.fixtures.synthetic_pipeline import link_series_graph_template

    config = MagicMock()
    link_series_graph_template(tmp_path)
    config.repo_root = tmp_path
    config.package_root = tmp_path / "dist" / "pkg"
    config.dist_root = tmp_path / "dist"
    config.dist_root.mkdir(parents=True)
    config.package_root.mkdir(parents=True)
    config.targets = ("Sheet!A1",)
    config.constraints = {}
    config.internal_binding_validation_mode = "warn"
    config.internal_binding_exempt_cells = frozenset()
    config.guide_path = tmp_path / "guide.md"
    config.guide_path.write_text("guide", encoding="utf-8")
    config.workbook_path = tmp_path / "workbook.xlsx"
    config.workbook_path.write_bytes(b"workbook")
    config.bindings_path = tmp_path / "bindings"
    config.bindings_path.mkdir()
    (config.bindings_path / "inputs.bindings.yaml").write_text(
        "series: []\n", encoding="utf-8"
    )
    config.graph_output_dir = tmp_path / "artifacts"
    config.dist_metadata = MagicMock()
    config.dist_metadata.package_name = "pkg"
    config.differential_workbook_rel = Path("data/workbook.xlsx")
    config.graph_output_dir.mkdir()

    graph_result = MagicMock()
    graph_result.graph = MagicMock()
    graph_result.series_bindings = MagicMock()
    graph_result.graph_cache_key = "graph-key"
    graph_result.internal_series = []
    graph_result.internal_binding_index = {}
    graph_result.bound_address_keys = {}
    graph_result.address_to_series_id = {}

    codegen_dir = tmp_path / "codegen"
    sample = dict(_SAMPLE_MODULES)

    with (
        patch(
            "src.extraction_pipeline.build_pipeline_graph",
            return_value=graph_result,
        ),
        patch("src.extraction_pipeline.profile_if_enabled") as profile_mock,
        patch("src.extraction_pipeline.configure_logging"),
        patch("src.extraction_pipeline.StageTimer") as timer_cls,
        patch("src.extraction_pipeline.resolve_stall_log_path") as stall_path,
        patch("src.package_materialize.seed_validation_harness"),
        patch("src.package_materialize.render_dist_pyproject_toml", return_value=""),
        patch("src.package_materialize.write_dist_readme"),
        patch("src.extraction_pipeline.CodeGenerator") as codegen_cls,
        patch.object(codegen_cache, "DEFAULT_CODEGEN_CACHE_DIR", codegen_dir),
        patch(
            "src.extraction_pipeline.projection_cache_key",
            return_value="proj-key",
        ),
    ):
        profile_mock.return_value.__enter__ = MagicMock(return_value=None)
        profile_mock.return_value.__exit__ = MagicMock(return_value=False)
        timer = timer_cls.return_value
        timer.print_summary = MagicMock()
        stall_path.return_value = tmp_path / "missing-stall.log"

        generator = MagicMock()
        generator.generate_modules.return_value = sample
        codegen_cls.return_value.__enter__.return_value = generator
        codegen_cls.return_value.__exit__.return_value = False

        run_export_stage(config)
        run_export_stage(config)

    assert generator.generate_modules.call_count == 1
    call = generator.generate_modules.call_args
    assert call.args == ()
    assert "paradigm" not in call.kwargs
    assert (config.package_root / "api.py").read_text(encoding="utf-8") == sample[
        "api.py"
    ]
    assert list(codegen_dir.glob("*.pkl.gz"))
