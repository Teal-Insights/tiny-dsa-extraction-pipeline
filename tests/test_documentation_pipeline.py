from src.documentation_pipeline import (
    INTRODUCTION_FOCUS_INSTRUCTIONS,
    build_section_prompt,
)


def test_section_prompt_limits_runnable_dependencies():
    prompt = build_section_prompt(
        section_name="Example",
        source_section_markdown="Source",
        python_focus_instructions="Instructions",
        pipeline_context_blocks={"context": "Pipeline context"},
        api_signatures="def make_context(): ...",
        response_schema={"type": "object"},
    )

    assert (
        "Runnable code may only use Python standard library and pandas, polars, "
        "and matplotlib."
    ) in prompt


def test_introduction_prompt_mentions_install_source_and_provenance():
    assert "uv" in INTRODUCTION_FOCUS_INSTRUCTIONS
    assert (
        "https://github.com/Teal-Insights/py-tiny-dsa"
        in INTRODUCTION_FOCUS_INSTRUCTIONS
    )
    assert "Python reimplementation" in INTRODUCTION_FOCUS_INSTRUCTIONS
    assert "programmatic extraction, machine translation, and AI" in (
        INTRODUCTION_FOCUS_INSTRUCTIONS
    )
