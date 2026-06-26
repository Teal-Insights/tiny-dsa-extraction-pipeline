"""Generate Great Docs content and CI workflow for exported docs."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from src.llm_json import generate_validated_json
from src.qmd_python_validation import validate_qmd_files

repo_root = Path(__file__).resolve().parents[1]
dist_root = repo_root / "dist"
guide_path = repo_root / "data" / "tiny-dsa-guide.md"
pipeline_doc_path = repo_root / "docs" / "extraction-pipeline.qmd"
api_module_path = dist_root / "tiny_dsa" / "api.py"
user_guide_root = dist_root / "user_guide"
rewrite_cache_path = repo_root / ".cache" / "guide-rewrites.json"
great_docs_yml = dist_root / "great-docs.yml"
docs_workflow_path = dist_root / ".github" / "workflows" / "deploy-docs.yml"

SECTION_REWRITE_MODEL = "gpt-5.5"
SECTION_REWRITE_PROMPT_VERSION = 4
MAX_SECTION_REWRITE_ATTEMPTS = 3
CANONICAL_API_USAGE_HEADING = "Canonical API usage"
NO_API_SIGNATURES = "No tiny_dsa.api symbols are required for this section."
VALIDATION_PAGE_FILENAME = "03-excel-parity-validation.qmd"
VALIDATION_REPORT_URL = (
    "https://github.com/Teal-Insights/py-tiny-dsa/blob/main/"
    "tests/results/reference/parity_report.txt"
)
VALIDATION_BUNDLE_README_URL = (
    "https://github.com/Teal-Insights/py-tiny-dsa/blob/main/tests/README.md"
)
VALIDATION_HARNESS_URL = (
    "https://github.com/Teal-Insights/py-tiny-dsa/blob/main/"
    "tests/differential_test_exported_library.py"
)

FUNCTIONAL_OVERVIEW_FOCUS_INSTRUCTIONS = (
    "Preserve section structure and conceptual flow, but replace workbook "
    "navigation and manual cell editing with tiny_dsa.api usage. "
    "Mirror the canonical_api_usage reference example for import style, "
    "ctx = make_context(), and compute_output_* calls. Setters accept "
    "whichever input shape reads most naturally: a bare scalar for "
    "single-cell setters, and records, a single record, a tidy Polars "
    "DataFrame, or a 1D sequence of measure values for multi-cell setters. "
    "Tabulate outputs with Polars using debt_to_gdp_frame-style select on OBS_VALUE."
)

ILLUSTRATIVE_EXAMPLE_FOCUS_INSTRUCTIONS = (
    "Keep the scenario faithful to the original narrative. "
    "Express each step with tiny_dsa.api using the same interaction model as "
    "canonical_api_usage, including debt_to_gdp_frame-style Polars tables for "
    "compute_output_* results. Split the workflow into several short runnable "
    "cells that reuse ctx = make_context(), passing each setter the input "
    "shape that reads most naturally: a bare scalar for single-cell setters, "
    "and records, a single record, a tidy Polars DataFrame, or a 1D sequence "
    "of measure values for multi-cell setters."
)

GREAT_DOCS_SETTINGS = [
    ("display_name", "Tiny DSA"),
    ("homepage", "user_guide"),
]

INTRODUCTION_FOCUS_INSTRUCTIONS = (
    "Rewrite the source introduction as the landing page for the generated "
    "Python package documentation. Recommend installing the package with uv "
    "from https://github.com/Teal-Insights/py-tiny-dsa. Clarify that Tiny DSA "
    "is a Python reimplementation of the illustrative Excel workbook, produced "
    "using a combination of programmatic extraction, machine translation, and AI. "
    "Keep the provenance concise and do not add pipeline details beyond that summary."
)

FUNCTIONAL_OVERVIEW_API_SYMBOLS = [
    "make_context",
    "set_country_name",
    "set_growth_baseline",
    "set_interest_baseline",
    "set_primary_balance_baseline",
    "set_shock_year",
    "set_shock_type",
    "set_shock_magnitudes",
    "compute_output_baseline",
    "compute_output_shocked",
    "compute_output_delta",
]

ILLUSTRATIVE_EXAMPLE_API_SYMBOLS = FUNCTIONAL_OVERVIEW_API_SYMBOLS


class SectionRewriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        description=("Section title in sentence case, without roman numeral prefixes.")
    )
    purpose: str = Field(
        description="One short sentence describing why this section matters."
    )
    rewritten_markdown: str = Field(
        description=(
            "Final Markdown body for the section without top-level heading. "
            "Include Python code examples where useful, and use Quarto runnable "
            "fences (` ```{python} `) for executable snippets."
        )
    )
    api_symbols_used: list[str] = Field(
        description="Symbols from tiny_dsa.api referenced in the rewritten section."
    )
    fidelity_notes: list[str] = Field(
        description=(
            "Short notes describing key Excel-to-Python rewrites while preserving intent."
        )
    )


@dataclass(frozen=True)
class ParityReportSummary:
    """Headline fields from the exported-library parity report."""

    generated: str
    tolerance: str
    total_comparisons: int
    passed: int
    failed: int
    pass_rate: str
    acceptance_bar: str
    result: str


def parse_parity_report(report_text: str) -> ParityReportSummary:
    """Parse the stable key-value header emitted by the parity report writer."""
    fields: dict[str, str] = {}
    for line in report_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()

    required = (
        "Generated",
        "Tolerance",
        "Total comparisons",
        "Passed",
        "Failed",
        "Pass rate",
        "Acceptance bar",
        "Result",
    )
    missing = [name for name in required if name not in fields]
    if missing:
        raise ValueError(f"Parity report missing required fields: {missing}")

    return ParityReportSummary(
        generated=fields["Generated"],
        tolerance=fields["Tolerance"],
        total_comparisons=int(fields["Total comparisons"]),
        passed=int(fields["Passed"]),
        failed=int(fields["Failed"]),
        pass_rate=fields["Pass rate"],
        acceptance_bar=fields["Acceptance bar"],
        result=fields["Result"],
    )


def render_validation_page(summary: ParityReportSummary) -> str:
    """Render the deterministic GreatDocs page for Excel parity validation."""
    passed = f"{summary.passed:,}"
    total = f"{summary.total_comparisons:,}"
    failed = f"{summary.failed:,}"
    return f"""---
title: "Excel parity validation"
---

Tiny DSA includes an exported validation bundle that checks the generated
`tiny_dsa` package against the illustrative Excel workbook. The test drives
the workbook with Microsoft Excel through `xlwings`, applies the same inputs
through the package's public `set_*` functions, and compares the public
`compute_*` outputs cell by cell.

## Current reference result

The current reference run is **{summary.result}**: **{passed} / {total}**
cell-level comparisons passed at `{summary.tolerance}`.

- Result: **{summary.result}**
- Generated: `{summary.generated}`
- Passed: **{passed}**
- Failed: **{failed}**
- Pass rate: **{summary.pass_rate}**
- Acceptance bar: **{summary.acceptance_bar}**

The sweep covers 118 scenarios and 15 output cells, for 1,770 comparisons
against Excel.

## What Was Tested

The validation checks the exported standalone library, not just the extraction
graph. It imports `tiny_dsa.api`, creates a fresh context for each scenario,
sets inputs through the records-shaped public setters, computes the exported
output series, and compares those values against the workbook's calculated
output cells.

## Inspect Or Re-run

The validation bundle is shipped in the source repository under `tests/`.
Because the golden-master oracle uses Microsoft Excel through COM automation,
reruns require Windows with Microsoft Excel installed.

- [Reference parity report]({VALIDATION_REPORT_URL})
- [Validation bundle README]({VALIDATION_BUNDLE_README_URL})
- [Differential test harness]({VALIDATION_HARNESS_URL})

To re-run the validation from the exported project:

```pwsh
uv run --project . --group validation python tests/differential_test_exported_library.py --layout exported
```
"""


def render_introduction_validation_note() -> str:
    """Return a short deterministic landing-page pointer to validation evidence."""
    return (
        "For correctness evidence, see "
        f"[Excel parity validation]({VALIDATION_PAGE_FILENAME}), which summarizes "
        "the exported-library differential test against the source workbook."
    )


def write_validation_page(
    *,
    dist_root_path: Path = dist_root,
    user_guide_root_path: Path = user_guide_root,
) -> None:
    """Write the deterministic user-guide page from exported validation assets."""
    report_path = (
        dist_root_path / "tests" / "results" / "reference" / "parity_report.txt"
    )
    readme_path = dist_root_path / "tests" / "README.md"
    if not readme_path.is_file():
        raise FileNotFoundError(f"Validation README not found: {readme_path}")
    if not report_path.is_file():
        raise FileNotFoundError(f"Reference parity report not found: {report_path}")

    summary = parse_parity_report(report_path.read_text(encoding="utf-8"))
    user_guide_root_path.mkdir(parents=True, exist_ok=True)
    (user_guide_root_path / VALIDATION_PAGE_FILENAME).write_text(
        render_validation_page(summary),
        encoding="utf-8",
    )


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def load_rewrite_cache() -> dict[str, str]:
    if not rewrite_cache_path.exists():
        return {}
    return json.loads(rewrite_cache_path.read_text(encoding="utf-8"))


def save_rewrite_cache(cache: dict[str, str]) -> None:
    rewrite_cache_path.parent.mkdir(parents=True, exist_ok=True)
    rewrite_cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def extract_markdown_section(markdown_text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, markdown_text, flags=re.DOTALL | re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find markdown heading: {heading}")
    return match.group(1).strip()


def extract_qmd_section(qmd_text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, qmd_text, flags=re.DOTALL | re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find qmd heading: {heading}")
    return match.group(1).strip()


def load_canonical_api_example(pipeline_doc_text: str) -> str:
    return extract_qmd_section(pipeline_doc_text, CANONICAL_API_USAGE_HEADING)


def canonical_api_context(pipeline_doc_text: str) -> dict[str, str]:
    return {
        "canonical_api_usage": load_canonical_api_example(pipeline_doc_text),
    }


def extract_api_signatures(api_path: Path, symbol_names: list[str]) -> str:
    source = api_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    blocks: list[str] = []
    lines = source.splitlines()
    wanted = set(symbol_names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            start = node.lineno - 1
            end = node.end_lineno
            blocks.append("\n".join(lines[start:end]))
    if not blocks:
        raise ValueError(f"No signatures found for symbols: {symbol_names}")
    return "\n\n".join(blocks)


def build_section_prompt(
    *,
    section_name: str,
    source_section_markdown: str,
    python_focus_instructions: str,
    pipeline_context_blocks: dict[str, str],
    api_signatures: str,
    response_schema: dict,
) -> str:
    reference_blocks = "\n\n".join(
        [f"[{label}]\n{text}" for label, text in pipeline_context_blocks.items()]
    )
    return f"""
Rewrite one section from the Tiny-DSA guide into Python-first documentation for a generated library website.

Goals:
- Stay faithful to the source section's structure and intent.
- Replace Excel workbook/user-interface instructions with Python API usage.
- Keep tone clear, concise, and production-ready.
- Match the import and call style in the reference example for every runnable cell.

Reference example (follow this interaction model):
{reference_blocks}

Hard constraints:
- Do not invent API symbols.
- Do not mention internal pipeline implementation details unless explicitly present in provided context.
- Do not include claims that conflict with provided API signatures.
- For runnable code examples, use Quarto executable fences exactly as ` ```{{python}} ` and not ` ```python `.
- Return valid JSON matching the response schema exactly.
- Runnable code may only use Python standard library, polars, and matplotlib.
- Tabulate compute_output_* results with polars, following the reference example.
- Use matplotlib when plots are needed.

Section name: {section_name}

Source section:
{source_section_markdown}

Python focus instructions:
{python_focus_instructions}

tiny_dsa.api signatures:
{api_signatures}

Response schema:
{json.dumps(response_schema, indent=2)}
""".strip()


def rewrite_cache_key(
    *,
    section_id: str,
    source_section_markdown: str,
    python_focus_instructions: str,
    pipeline_context_blocks: dict[str, str],
    api_signatures: str,
    response_schema: dict,
) -> str:
    payload = {
        "model": SECTION_REWRITE_MODEL,
        "prompt_version": SECTION_REWRITE_PROMPT_VERSION,
        "section_id": section_id,
        "source_section_markdown": source_section_markdown,
        "python_focus_instructions": python_focus_instructions,
        "pipeline_context_blocks": pipeline_context_blocks,
        "api_signatures": api_signatures,
        "response_schema": response_schema,
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def qmd_body_without_frontmatter(qmd_text: str) -> str:
    """Return the body of a QMD page that starts with YAML frontmatter."""
    match = re.match(r"^---\n.*?\n---\n\n(?P<body>.*)\Z", qmd_text, re.DOTALL)
    if match is None:
        raise ValueError("Expected QMD text to start with YAML frontmatter")
    return match.group("body").rstrip("\n")


def sync_cached_rewrite_from_qmd(
    *,
    cache_path: Path,
    cache_key: str,
    qmd_path: Path,
) -> bool:
    """Persist a validated QMD body back into the matching rewrite-cache entry."""
    if not cache_path.is_file() or not qmd_path.is_file():
        return False

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cached_json = cache.get(cache_key)
    if cached_json is None:
        return False

    cached_response = SectionRewriteResponse.model_validate_json(cached_json)
    validated_body = qmd_body_without_frontmatter(qmd_path.read_text(encoding="utf-8"))
    if cached_response.rewritten_markdown == validated_body:
        return False

    updated_response = cached_response.model_copy(
        update={"rewritten_markdown": validated_body}
    )
    cache[cache_key] = updated_response.model_dump_json(indent=2)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return True


def sync_validated_pages_to_rewrite_cache(
    *,
    guide_text: str,
    pipeline_doc_text: str,
) -> None:
    """Update cached guide rewrites from validated generated QMD pages.

    Only pure LLM-authored pages are synced. ``index.qmd`` gets a deterministic
    validation note appended outside the cached rewrite and is intentionally
    excluded.
    """
    response_schema = SectionRewriteResponse.model_json_schema()
    functional_key = rewrite_cache_key(
        section_id="functional_overview",
        source_section_markdown=extract_markdown_section(
            guide_text, "II. Functional Overview"
        ),
        python_focus_instructions=FUNCTIONAL_OVERVIEW_FOCUS_INSTRUCTIONS,
        pipeline_context_blocks=canonical_api_context(pipeline_doc_text),
        api_signatures=extract_api_signatures(
            api_module_path, FUNCTIONAL_OVERVIEW_API_SYMBOLS
        ),
        response_schema=response_schema,
    )
    illustrative_key = rewrite_cache_key(
        section_id="illustrative_example",
        source_section_markdown=extract_markdown_section(
            guide_text, "III. Illustrative Example"
        ),
        python_focus_instructions=ILLUSTRATIVE_EXAMPLE_FOCUS_INSTRUCTIONS,
        pipeline_context_blocks=canonical_api_context(pipeline_doc_text),
        api_signatures=extract_api_signatures(
            api_module_path, ILLUSTRATIVE_EXAMPLE_API_SYMBOLS
        ),
        response_schema=response_schema,
    )

    sync_cached_rewrite_from_qmd(
        cache_path=rewrite_cache_path,
        cache_key=functional_key,
        qmd_path=user_guide_root / "01-functional-overview.qmd",
    )
    sync_cached_rewrite_from_qmd(
        cache_path=rewrite_cache_path,
        cache_key=illustrative_key,
        qmd_path=user_guide_root / "02-illustrative-example.qmd",
    )


_BARE_CELL_FENCE = re.compile(r"^\{[A-Za-z][\w-]*\}$")


def validate_rewritten_markdown_fences(markdown: str) -> None:
    """Reject markdown whose code cells are not wrapped in triple-backtick fences.

    Models occasionally emit a bare ``{python}`` line instead of an opening
    ```` ```{python} ```` fence, which Quarto renders as plain text and which
    silently skips runnable-cell validation. This checks that every cell fence is
    backtick-delimited and that fences are balanced.

    Args:
        markdown: The rewritten section body to validate.

    Raises:
        ValueError: If a bare cell fence is found outside a fenced block, or if
            the triple-backtick fences are unbalanced.
    """
    fence_count = 0
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fence_count += 1
            in_fence = not in_fence
            continue
        if not in_fence and _BARE_CELL_FENCE.match(stripped):
            raise ValueError(
                f"Found a bare cell fence line {stripped!r} without enclosing "
                "triple backticks; runnable cells must use Quarto fences such as "
                "```{python} ... ```."
            )
    if fence_count % 2 != 0:
        raise ValueError(
            "Unbalanced code fences: found an odd number of ``` markers; every "
            "opening ```{python} fence must have a matching closing ```."
        )


def _validate_section_rewrite(parsed: SectionRewriteResponse) -> SectionRewriteResponse:
    validate_rewritten_markdown_fences(parsed.rewritten_markdown)
    return parsed


def rewrite_guide_section(
    *,
    client: OpenAI | None,
    section_id: str,
    section_name: str,
    source_section_markdown: str,
    python_focus_instructions: str,
    pipeline_context_blocks: dict[str, str],
    api_signatures: str,
) -> SectionRewriteResponse:
    response_schema = SectionRewriteResponse.model_json_schema()
    cache = load_rewrite_cache()
    cache_key = rewrite_cache_key(
        section_id=section_id,
        source_section_markdown=source_section_markdown,
        python_focus_instructions=python_focus_instructions,
        pipeline_context_blocks=pipeline_context_blocks,
        api_signatures=api_signatures,
        response_schema=response_schema,
    )
    if cache_key in cache:
        return SectionRewriteResponse.model_validate_json(cache[cache_key])

    if client is None:
        raise RuntimeError(
            "OPENAI_API_KEY is required to generate uncached guide rewrites"
        )

    prompt = build_section_prompt(
        section_name=section_name,
        source_section_markdown=source_section_markdown,
        python_focus_instructions=python_focus_instructions,
        pipeline_context_blocks=pipeline_context_blocks,
        api_signatures=api_signatures,
        response_schema=response_schema,
    )
    parsed, content = generate_validated_json(
        client=client,
        model=SECTION_REWRITE_MODEL,
        system_prompt=(
            "You are a technical documentation writer for the tiny_dsa library. "
            "Runnable examples use tiny_dsa.api with make_context(), "
            "records-shaped setters, and compute_output_* functions, "
            "as shown in the reference example. "
            "Return only valid JSON matching the provided schema."
        ),
        user_prompt=prompt,
        response_model=SectionRewriteResponse,
        post_validate=_validate_section_rewrite,
        max_attempts=MAX_SECTION_REWRITE_ATTEMPTS,
    )
    cache[cache_key] = content
    save_rewrite_cache(cache)
    return parsed


def has_top_level_key(yaml_content: str, key: str) -> bool:
    for line in yaml_content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}:"):
            return True
    return False


def configure_great_docs_yml() -> None:
    content = great_docs_yml.read_text(encoding="utf-8")
    content = content.replace("# module: yaml12", "module: tiny_dsa")

    insert_lines = [
        f"{key}: {value}"
        for key, value in GREAT_DOCS_SETTINGS
        if not has_top_level_key(content, key)
    ]
    if insert_lines:
        if "module: tiny_dsa" in content:
            content = content.replace(
                "module: tiny_dsa",
                "module: tiny_dsa\n" + "\n".join(insert_lines),
                1,
            )
        else:
            content = content.rstrip() + "\n\n" + "\n".join(insert_lines) + "\n"

    great_docs_yml.write_text(content, encoding="utf-8")


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> None:
    command_env = os.environ.copy()
    if extra_env is not None:
        command_env.update(extra_env)
    subprocess.run(
        args,
        check=True,
        env=command_env,
        cwd=str(cwd) if cwd is not None else None,
    )


def write_introduction_page(client: OpenAI | None, guide_text: str) -> None:
    introduction_source = extract_markdown_section(guide_text, "I. Introduction[^1]")
    introduction_rewrite = rewrite_guide_section(
        client=client,
        section_id="introduction",
        section_name="Introduction",
        source_section_markdown=introduction_source,
        python_focus_instructions=INTRODUCTION_FOCUS_INSTRUCTIONS,
        pipeline_context_blocks={},
        api_signatures=NO_API_SIGNATURES,
    )
    user_guide_root.mkdir(parents=True, exist_ok=True)
    landing_page_output = user_guide_root / "index.qmd"
    landing_page_qmd = f"""---
title: "{introduction_rewrite.title}"
---

{introduction_rewrite.rewritten_markdown}

{render_introduction_validation_note()}
"""
    landing_page_output.write_text(landing_page_qmd, encoding="utf-8")


def write_rewritten_guide_pages(client: OpenAI | None) -> None:
    guide_text = guide_path.read_text(encoding="utf-8")
    pipeline_doc_text = pipeline_doc_path.read_text(encoding="utf-8")

    functional_overview_source = extract_markdown_section(
        guide_text,
        "II. Functional Overview",
    )
    functional_overview_context = canonical_api_context(pipeline_doc_text)
    functional_overview_api = extract_api_signatures(
        api_module_path,
        FUNCTIONAL_OVERVIEW_API_SYMBOLS,
    )
    functional_overview_rewrite = rewrite_guide_section(
        client=client,
        section_id="functional_overview",
        section_name="Functional Overview",
        source_section_markdown=functional_overview_source,
        python_focus_instructions=FUNCTIONAL_OVERVIEW_FOCUS_INSTRUCTIONS,
        pipeline_context_blocks=functional_overview_context,
        api_signatures=functional_overview_api,
    )

    illustrative_example_source = extract_markdown_section(
        guide_text,
        "III. Illustrative Example",
    )
    illustrative_example_context = canonical_api_context(pipeline_doc_text)
    illustrative_example_api = extract_api_signatures(
        api_module_path,
        ILLUSTRATIVE_EXAMPLE_API_SYMBOLS,
    )
    illustrative_example_rewrite = rewrite_guide_section(
        client=client,
        section_id="illustrative_example",
        section_name="Illustrative Example",
        source_section_markdown=illustrative_example_source,
        python_focus_instructions=ILLUSTRATIVE_EXAMPLE_FOCUS_INSTRUCTIONS,
        pipeline_context_blocks=illustrative_example_context,
        api_signatures=illustrative_example_api,
    )

    user_guide_root.mkdir(parents=True, exist_ok=True)
    functional_overview_output = user_guide_root / "01-functional-overview.qmd"
    functional_overview_output.write_text(
        f"""---
title: "{functional_overview_rewrite.title}"
---

{functional_overview_rewrite.rewritten_markdown}
""",
        encoding="utf-8",
    )

    illustrative_example_output = user_guide_root / "02-illustrative-example.qmd"
    illustrative_example_output.write_text(
        f"""---
title: "{illustrative_example_rewrite.title}"
---

{illustrative_example_rewrite.rewritten_markdown}
""",
        encoding="utf-8",
    )


def write_docs_deploy_workflow() -> None:
    docs_workflow_path.parent.mkdir(parents=True, exist_ok=True)
    docs_workflow = """name: Build and deploy docs

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v6

      - name: Set up Pages
        uses: actions/configure-pages@v5

      - name: Install uv
        uses: astral-sh/setup-uv@v4

      - name: Install Quarto
        uses: quarto-dev/quarto-actions/setup@v2

      - name: Set up Python
        run: uv python install

      - name: Install project dependencies
        run: uv sync --group dev

      - name: Build documentation site
        run: uv run --with great-docs great-docs build --project-path .

      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@v4
        with:
          path: great-docs/_site

      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
"""
    docs_workflow_path.write_text(docs_workflow, encoding="utf-8")


def run_documentation_pipeline() -> None:
    if not great_docs_yml.exists():
        run_cmd(
            [
                "uv",
                "run",
                "--project",
                str(dist_root),
                "--with",
                "great-docs",
                "great-docs",
                "init",
                "--project-path",
                str(dist_root),
            ]
        )

    configure_great_docs_yml()

    api_key = os.environ.get("OPENAI_API_KEY")
    section_client = (
        OpenAI(api_key=api_key, base_url="https://api.openai.com/v1/")
        if api_key
        else None
    )
    guide_text = guide_path.read_text(encoding="utf-8")
    write_introduction_page(section_client, guide_text)
    write_rewritten_guide_pages(section_client)
    write_validation_page()
    validate_qmd_files(
        dist_root=dist_root,
        qmd_paths=sorted(user_guide_root.glob("*.qmd")),
        client=section_client,
    )
    pipeline_doc_text = pipeline_doc_path.read_text(encoding="utf-8")
    sync_validated_pages_to_rewrite_cache(
        guide_text=guide_text,
        pipeline_doc_text=pipeline_doc_text,
    )
    write_docs_deploy_workflow()
