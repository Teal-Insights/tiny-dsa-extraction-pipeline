from pathlib import Path

from src.documentation_pipeline import (
    CANONICAL_API_USAGE_HEADING,
    INTRODUCTION_FOCUS_INSTRUCTIONS,
    build_section_prompt,
    load_canonical_api_example,
    pipeline_doc_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_canonical_api_example_reads_qmd_section() -> None:
    example = load_canonical_api_example(pipeline_doc_path.read_text(encoding="utf-8"))
    assert "make_context()" in example
    assert "from tiny_dsa.api import" in example
    assert "set_country_name" in example
    assert "compute_output_baseline" in example
    assert "import polars as pl" in example
    assert 'pl.col("OBS_VALUE")' in example
    assert "debt_to_gdp_frame" in example


def test_section_prompt_includes_reference_example_block() -> None:
    prompt = build_section_prompt(
        section_name="Functional Overview",
        source_section_markdown="Source",
        python_focus_instructions="Mirror the canonical example.",
        pipeline_context_blocks={
            "canonical_api_usage": "ctx = make_context()",
        },
        api_signatures="def make_context(): ...",
        response_schema={"type": "object"},
    )

    assert "Reference example" in prompt
    assert "[canonical_api_usage]" in prompt
    assert "ctx = make_context()" in prompt
    assert "Match the import and call style" in prompt


def test_section_prompt_limits_runnable_dependencies() -> None:
    prompt = build_section_prompt(
        section_name="Example",
        source_section_markdown="Source",
        python_focus_instructions="Instructions",
        pipeline_context_blocks={"canonical_api_usage": "ctx = make_context()"},
        api_signatures="def make_context(): ...",
        response_schema={"type": "object"},
    )

    assert (
        "Runnable code may only use Python standard library, polars, and matplotlib."
    ) in prompt
    assert "Tabulate compute_output_* results with polars" in prompt


def test_canonical_api_usage_heading_matches_qmd() -> None:
    qmd_text = pipeline_doc_path.read_text(encoding="utf-8")
    assert f"## {CANONICAL_API_USAGE_HEADING}" in qmd_text


def test_introduction_prompt_mentions_install_source_and_provenance() -> None:
    assert "uv" in INTRODUCTION_FOCUS_INSTRUCTIONS
    assert (
        "https://github.com/Teal-Insights/py-tiny-dsa"
        in INTRODUCTION_FOCUS_INSTRUCTIONS
    )
    assert "Python reimplementation" in INTRODUCTION_FOCUS_INSTRUCTIONS
    assert "programmatic extraction, machine translation, and AI" in (
        INTRODUCTION_FOCUS_INSTRUCTIONS
    )
