from pathlib import Path

import pytest

from src.pipeline_config import (
    PipelineConfig,
    discover_public_api_symbols,
    load_pipeline_config,
)
from src.documentation_pipeline import (
    SETTER_INPUT_SHAPE_GUIDANCE,
    build_section_prompt,
    extract_api_signatures,
    introduction_focus_instructions,
    functional_overview_focus_instructions,
    illustrative_example_focus_instructions,
    load_canonical_api_example,
    parse_parity_report,
    render_validation_page,
    validate_rewritten_markdown_fences,
)


def test_load_canonical_api_example_reads_template() -> None:
    config = load_pipeline_config()
    example = load_canonical_api_example(config)
    assert "make_context()" in example
    assert "compute_" in example
    assert "import polars as pl" in example


def test_section_focus_templates_use_placeholders() -> None:
    config = load_pipeline_config()

    introduction = introduction_focus_instructions(config)
    assert config.dist_metadata.library_name in introduction
    assert config.dist_metadata.resolved_install_command() in introduction

    functional = functional_overview_focus_instructions(config)
    illustrative = illustrative_example_focus_instructions(config)
    assert config.api_import_path in functional
    assert config.api_import_path in illustrative
    assert "canonical_api_usage" in functional
    assert "canonical_api_usage" in illustrative
    assert "templates/canonical-api-usage.md" in functional
    assert "templates/canonical-api-usage.md" in illustrative


def test_build_section_prompt_includes_input_shape_guidance() -> None:
    config = load_pipeline_config()
    canonical = load_canonical_api_example(config)
    prompt = build_section_prompt(
        library_name=config.dist_metadata.library_name,
        api_import_path=config.api_import_path,
        section_name="Functional Overview",
        source_section_markdown="Source",
        python_focus_instructions="Mirror the canonical example.",
        pipeline_context_blocks={"canonical_api_usage": canonical},
        api_signatures="def make_context(): ...",
        response_schema={"type": "object"},
    )

    assert config.api_import_path in prompt
    assert canonical in prompt
    assert SETTER_INPUT_SHAPE_GUIDANCE in prompt
    assert "bare scalar" in prompt
    assert "Polars DataFrame" in prompt
    assert "ctx = make_context()" in prompt


def test_build_section_prompt_uses_discovered_api_signatures(tmp_path: Path) -> None:
    api_path = tmp_path / "api.py"
    api_path.write_text(
        "\n".join(
            [
                "def make_context():",
                "    return {}",
                "",
                "def set_example(ctx, value):",
                "    pass",
                "",
                "def compute_example(ctx):",
                "    return []",
            ]
        ),
        encoding="utf-8",
    )
    symbols = list(discover_public_api_symbols(api_path))
    signatures = extract_api_signatures(api_path, symbols)

    prompt = build_section_prompt(
        library_name="Example Model",
        api_import_path="example_model.api",
        section_name="Functional Overview",
        source_section_markdown="Source",
        python_focus_instructions="Focus",
        pipeline_context_blocks={"canonical_api_usage": "ctx = make_context()"},
        api_signatures=signatures,
        response_schema={"type": "object"},
    )

    assert "def make_context():" in prompt
    assert "def set_example(ctx, value):" in prompt
    assert "def compute_example(ctx):" in prompt


def test_fence_validation_accepts_well_formed_quarto_cell() -> None:
    markdown = (
        "Intro paragraph.\n\n"
        "```{python}\n"
        "from my_model.api import make_context\n"
        "ctx = make_context()\n"
        "```\n\n"
        "Closing paragraph."
    )
    validate_rewritten_markdown_fences(markdown)


def test_render_validation_page_uses_package_metadata() -> None:
    from src.documentation_pipeline import ParityReportSummary

    summary = ParityReportSummary(
        generated="2026-01-01",
        tolerance="1e-6",
        total_comparisons=10,
        passed=10,
        failed=0,
        pass_rate="100%",
        acceptance_bar="100%",
        result="PASS",
    )
    page = render_validation_page(
        summary,
        library_name="Forecast Kit",
        package_name="forecast_kit",
    )
    assert "Forecast Kit" in page
    assert "forecast_kit" in page


def test_parse_parity_report() -> None:
    report = """Generated: 2026-01-01
Tolerance: 1e-6
Total comparisons: 10
Passed: 10
Failed: 0
Pass rate: 100%
Acceptance bar: 100%
Result: PASS
"""
    summary = parse_parity_report(report)
    assert summary.result == "PASS"
    assert summary.total_comparisons == 10


def test_discover_public_api_symbols_from_generated_api(tmp_path: Path) -> None:
    api_path = tmp_path / "api.py"
    api_path.write_text(
        "def make_context():\n    return {}\n\n"
        "def _internal():\n    pass\n\n"
        "def compute_output():\n    return []\n",
        encoding="utf-8",
    )
    assert discover_public_api_symbols(api_path) == ("compute_output", "make_context")


@pytest.fixture
def section_focus_config(tmp_path: Path) -> PipelineConfig:
    config = load_pipeline_config(repo_root=tmp_path)
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "section-rewrite-introduction-focus.txt").write_text(
        "Install with `{install}` for {library_name}.",
        encoding="utf-8",
    )
    (templates_dir / "section-rewrite-functional-overview-focus.txt").write_text(
        "Use {api_import_path} and {canonical_api_example_path}.",
        encoding="utf-8",
    )
    (templates_dir / "section-rewrite-illustrative-example-focus.txt").write_text(
        "Scenario via {api_import_path} and {canonical_api_example_path}.",
        encoding="utf-8",
    )
    return config


def test_section_focus_templates_are_workbook_overridable(
    section_focus_config: PipelineConfig,
) -> None:
    config = section_focus_config
    assert "Install with" in introduction_focus_instructions(config)
    assert config.api_import_path in functional_overview_focus_instructions(config)
    assert config.repo_relative_posix_path(
        config.canonical_api_example_path
    ) in functional_overview_focus_instructions(config)
