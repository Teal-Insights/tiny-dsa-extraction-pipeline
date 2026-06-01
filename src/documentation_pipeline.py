"""Generate GreatDocs site content and build the exported library documentation."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

repo_root = Path(__file__).resolve().parents[1]
dist_root = repo_root / "dist"
guide_path = repo_root / "data" / "tiny-dsa-guide.md"
pipeline_doc_path = repo_root / "docs" / "extraction-pipeline.qmd"
api_module_path = dist_root / "tiny_dsa" / "api.py"
user_guide_root = dist_root / "user_guide"
rewrite_cache_path = repo_root / ".cache" / "guide-rewrites.json"
great_docs_yml = dist_root / "great-docs.yml"

SECTION_REWRITE_MODEL = "deepseek-v4-pro"
SECTION_REWRITE_PROMPT_VERSION = 2

GREAT_DOCS_SETTINGS = [
    ("display_name", "Tiny DSA"),
    ("homepage", "user_guide"),
]

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
    context_text = "\n\n".join(
        [f"[{label}]\n{text}" for label, text in pipeline_context_blocks.items()]
    )
    return f"""
Rewrite one section from the Tiny-DSA guide into Python-first documentation for a generated library website.

Goals:
- Stay faithful to the source section's structure and intent.
- Replace Excel workbook/user-interface instructions with Python API usage.
- Keep tone clear, concise, and production-ready.
- Prefer concrete `tiny_dsa.api` examples over abstract statements.

Hard constraints:
- Do not invent API symbols.
- Do not mention internal pipeline implementation details unless explicitly present in provided context.
- Do not include claims that conflict with provided API signatures.
- For runnable code examples, use Quarto executable fences exactly as ` ```{{python}} ` and not ` ```python `.
- Return valid JSON matching the response schema exactly.

Section name: {section_name}

Source section:
{source_section_markdown}

Python focus instructions:
{python_focus_instructions}

Pipeline context:
{context_text}

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
            "DEEPSEEK_API_KEY is required to generate uncached guide rewrites"
        )

    prompt = build_section_prompt(
        section_name=section_name,
        source_section_markdown=source_section_markdown,
        python_focus_instructions=python_focus_instructions,
        pipeline_context_blocks=pipeline_context_blocks,
        api_signatures=api_signatures,
        response_schema=response_schema,
    )
    response = client.chat.completions.create(
        model=SECTION_REWRITE_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a technical documentation writer for Python libraries. "
                    "Return only valid JSON matching the provided schema."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        stream=False,
        reasoning_effort="high",
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "enabled"}},
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("LLM returned empty section rewrite response")
    parsed = SectionRewriteResponse.model_validate_json(content)
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
    command_env["PYTHONIOENCODING"] = "utf-8"
    if extra_env is not None:
        command_env.update(extra_env)
    subprocess.run(
        args,
        check=True,
        env=command_env,
        cwd=str(cwd) if cwd is not None else None,
    )


def write_introduction_page(guide_text: str) -> None:
    introduction_source = extract_markdown_section(guide_text, "I. Introduction[^1]")
    user_guide_root.mkdir(parents=True, exist_ok=True)
    landing_page_output = user_guide_root / "index.qmd"
    landing_page_qmd = f"""---
title: "Introduction"
---

{introduction_source}
"""
    landing_page_output.write_text(landing_page_qmd, encoding="utf-8")


def write_rewritten_guide_pages(client: OpenAI | None) -> None:
    guide_text = guide_path.read_text(encoding="utf-8")
    pipeline_doc_text = pipeline_doc_path.read_text(encoding="utf-8")

    functional_overview_source = extract_markdown_section(
        guide_text,
        "II. Functional Overview",
    )
    functional_overview_context = {
        "pipeline_stage_2b_export": extract_qmd_section(
            pipeline_doc_text,
            "Stage 2B: Export",
        ),
        "pipeline_stage_3_test": extract_qmd_section(
            pipeline_doc_text,
            "Stage 3: Test",
        ),
    }
    functional_overview_api = extract_api_signatures(
        api_module_path,
        FUNCTIONAL_OVERVIEW_API_SYMBOLS,
    )
    functional_overview_rewrite = rewrite_guide_section(
        client=client,
        section_id="functional_overview",
        section_name="Functional Overview",
        source_section_markdown=functional_overview_source,
        python_focus_instructions=(
            "Preserve section structure and conceptual flow, but replace workbook "
            "navigation and manual cell editing with direct function calls to "
            "tiny_dsa.api. Explain records-shaped setters and semantic compute "
            "functions. Include one concise Python usage snippet."
        ),
        pipeline_context_blocks=functional_overview_context,
        api_signatures=functional_overview_api,
    )

    illustrative_example_source = extract_markdown_section(
        guide_text,
        "III. Illustrative Example",
    )
    illustrative_example_context = {
        "pipeline_stage_3_test": extract_qmd_section(
            pipeline_doc_text,
            "Stage 3: Test",
        ),
    }
    illustrative_example_api = extract_api_signatures(
        api_module_path,
        ILLUSTRATIVE_EXAMPLE_API_SYMBOLS,
    )
    illustrative_example_rewrite = rewrite_guide_section(
        client=client,
        section_id="illustrative_example",
        section_name="Illustrative Example",
        source_section_markdown=illustrative_example_source,
        python_focus_instructions=(
            "Keep the scenario faithful to the original narrative, but express each "
            "step using tiny_dsa.api function calls and records-based inputs. "
            "Include a complete runnable snippet that sets assumptions and computes "
            "baseline, shocked, and delta outputs."
        ),
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


def build_great_docs_site() -> None:
    run_cmd(
        [
            "uv",
            "run",
            "--project",
            str(dist_root),
            "--with",
            "great-docs",
            "great-docs",
            "build",
            "--project-path",
            str(dist_root),
        ],
        extra_env={"GD_FREEZE_ONLY": "1"},
    )
    run_cmd(
        [
            "uv",
            "run",
            "--project",
            str(dist_root),
            "--with",
            "great-docs",
            "python",
            "scripts/post-render.py",
        ],
        cwd=dist_root / "great-docs",
        extra_env={"PYTHONUTF8": "1"},
    )


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

    guide_text = guide_path.read_text(encoding="utf-8")
    write_introduction_page(guide_text)

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    section_client = (
        OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        if api_key
        else None
    )
    write_rewritten_guide_pages(section_client)

    build_great_docs_site()
