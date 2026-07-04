"""Integration tests for config-scoped runtime symbol discovery."""

from __future__ import annotations

from pathlib import Path

from src.pipeline_config import DistProjectMetadata, PipelineConfig
from src.pipeline_context import activate_pipeline_config
from src.runtime_symbols import (
    allowed_runtime_symbols,
    discover_allowed_runtime_symbols,
)


def _write_runtime(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "class XlError(Exception):",
                "    pass",
                "",
                "def to_bool(value):",
                "    return True",
                "",
                "def xl_eval():",
                "    return 1",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_allowed_runtime_symbols_uses_package_root(tmp_path: Path) -> None:
    package_root = tmp_path / "dist" / "my_model"
    _write_runtime(package_root / "runtime.py")
    config = PipelineConfig(
        repo_root=tmp_path,
        workbook_path=tmp_path / "workbook.xlsx",
        guide_path=tmp_path / "guide.md",
        bindings_path=tmp_path / "bindings",
        dist_root=tmp_path / "dist",
        targets=("Sheet1!A1",),
        constraints={"Sheet1!A1": "x"},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="test",
            documentation_url="https://example.com",
        ),
        docstring_callback_name="series_docs",
        projection_layout=None,
        canonical_api_example_path=tmp_path / "templates" / "canonical-api-usage.md",
        binding_authoring_prompt_path=tmp_path
        / "templates"
        / "binding-authoring-prompt.txt",
        section_rewrite_introduction_focus_path=(
            tmp_path / "templates" / "section-rewrite-introduction-focus.txt"
        ),
        section_rewrite_functional_overview_focus_path=(
            tmp_path / "templates" / "section-rewrite-functional-overview-focus.txt"
        ),
        section_rewrite_illustrative_example_focus_path=(
            tmp_path / "templates" / "section-rewrite-illustrative-example-focus.txt"
        ),
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=tmp_path / "artifacts" / "dependency-graph",
    )
    activate_pipeline_config(config)

    allowed_runtime_symbols.cache_clear()
    symbols = allowed_runtime_symbols()

    assert symbols == discover_allowed_runtime_symbols(
        config.package_root / "runtime.py"
    )
    assert "to_bool" not in symbols
    assert symbols == ("XlError", "xl_eval")
