import json
from pathlib import Path

import pytest

from src.documentation_pipeline import (
    CANONICAL_API_USAGE_HEADING,
    INTRODUCTION_FOCUS_INSTRUCTIONS,
    SectionRewriteResponse,
    build_section_prompt,
    load_canonical_api_example,
    parse_parity_report,
    pipeline_doc_path,
    render_validation_page,
    sync_cached_rewrite_from_qmd,
    validate_rewritten_markdown_fences,
    write_validation_page,
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


def test_fence_validation_accepts_well_formed_quarto_cell() -> None:
    markdown = (
        "Intro paragraph.\n\n"
        "```{python}\n"
        "from tiny_dsa.api import make_context\n"
        "ctx = make_context()\n"
        "```\n\n"
        "Closing paragraph."
    )
    validate_rewritten_markdown_fences(markdown)


def test_fence_validation_rejects_bare_python_fence() -> None:
    markdown = (
        "No separate API call is required.\n\n"
        "{python}\n"
        'set_country_name(ctx, "Litellia")\n\n\n'
        "The country profile table is small."
    )
    with pytest.raises(ValueError, match="bare cell fence"):
        validate_rewritten_markdown_fences(markdown)


def test_fence_validation_rejects_unbalanced_fence() -> None:
    markdown = "Intro.\n\n```{python}\nctx = make_context()\n\nNo closing fence."
    with pytest.raises(ValueError, match="[Uu]nbalanced"):
        validate_rewritten_markdown_fences(markdown)


def test_fence_validation_ignores_fence_token_inside_code_block() -> None:
    markdown = 'Intro.\n\n```{python}\nliteral = "{python}"\n```\n'
    validate_rewritten_markdown_fences(markdown)


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


def test_parse_parity_report_extracts_reference_result() -> None:
    summary = parse_parity_report(
        """Parity report: exported tiny_dsa standalone library vs Excel
Generated: 2026-06-03T22:27:37+00:00
Workbook: .\\tiny-dsa-extraction-pipeline\\data\\tiny-dsa.xlsx
Package:   .\\tiny-dsa-extraction-pipeline\\dist\\tiny_dsa (imported as dist.tiny_dsa.api)
Tolerance: atol = 1e-06

Total comparisons: 1770
Passed:            1770
Failed:            0
Pass rate:         100.00%
Acceptance bar:    100.00%
Result:            PASS
"""
    )

    assert summary.generated == "2026-06-03T22:27:37+00:00"
    assert summary.tolerance == "atol = 1e-06"
    assert summary.total_comparisons == 1770
    assert summary.passed == 1770
    assert summary.failed == 0
    assert summary.result == "PASS"


def test_render_validation_page_links_reference_report() -> None:
    summary = parse_parity_report(
        """Generated: 2026-06-03T22:27:37+00:00
Tolerance: atol = 1e-06
Total comparisons: 1770
Passed: 1770
Failed: 0
Pass rate: 100.00%
Acceptance bar: 100.00%
Result: PASS
"""
    )

    page = render_validation_page(summary)

    assert 'title: "Excel parity validation"' in page
    assert "1,770 / 1,770" in page
    assert "atol = 1e-06" in page
    assert "tests/results/reference/parity_report.txt" in page
    assert "Windows" in page
    assert "Microsoft Excel" in page


def test_write_validation_page_uses_exported_test_assets(tmp_path: Path) -> None:
    dist_root = tmp_path / "dist"
    user_guide_root = dist_root / "user_guide"
    report_path = dist_root / "tests" / "results" / "reference" / "parity_report.txt"
    readme_path = dist_root / "tests" / "README.md"
    report_path.parent.mkdir(parents=True)
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        """Generated: 2026-06-03T22:27:37+00:00
Tolerance: atol = 1e-06
Total comparisons: 1770
Passed: 1770
Failed: 0
Pass rate: 100.00%
Acceptance bar: 100.00%
Result: PASS
""",
        encoding="utf-8",
    )
    readme_path.write_text("# Excel parity validation\n", encoding="utf-8")

    write_validation_page(
        dist_root_path=dist_root,
        user_guide_root_path=user_guide_root,
    )

    page_path = user_guide_root / "03-excel-parity-validation.qmd"
    assert page_path.is_file()
    assert "Result: **PASS**" in page_path.read_text(encoding="utf-8")


def test_sync_cached_rewrite_from_qmd_persists_validated_body(tmp_path: Path) -> None:
    cache_path = tmp_path / "guide-rewrites.json"
    qmd_path = tmp_path / "02-illustrative-example.qmd"
    cache_key = "abc123"
    cached_response = SectionRewriteResponse(
        title="Illustrative example",
        purpose="Show the workflow.",
        rewritten_markdown="bad body",
        api_symbols_used=["make_context"],
        fidelity_notes=[],
    )
    cache_path.write_text(
        json.dumps({cache_key: cached_response.model_dump_json()}),
        encoding="utf-8",
    )
    qmd_path.write_text(
        """---
title: "Illustrative example"
---

fixed body

```{python}
print("validated")
```
""",
        encoding="utf-8",
    )

    updated = sync_cached_rewrite_from_qmd(
        cache_path=cache_path,
        cache_key=cache_key,
        qmd_path=qmd_path,
    )

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    response = SectionRewriteResponse.model_validate_json(cache[cache_key])
    assert updated is True
    assert (
        response.rewritten_markdown
        == 'fixed body\n\n```{python}\nprint("validated")\n```'
    )
    assert response.title == "Illustrative example"
