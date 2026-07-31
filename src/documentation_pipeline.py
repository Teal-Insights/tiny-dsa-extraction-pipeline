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

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from src.env_utils import env_float
from src.llm_json import generate_validated_json
from src.llm_providers import (
    model_from_env,
    provider_for_model,
)
from src.logging_config import configure_logging
from src.pipeline_config import (
    PipelineConfig,
    RunnableCellRule,
    discover_public_api_symbols,
)
from src.pipeline_monitor import (
    StageTimer,
    monitor_pipeline_stage,
    resolve_stall_log_path,
)
from src.qmd_python_validation import (
    PublicApiPolicy,
    extract_python_cells,
    validate_qmd_files,
)

SECTION_REWRITE_MODEL_ENV = "SECTION_REWRITE_MODEL"
SECTION_REWRITE_PROMPT_VERSION = 9
MAX_SECTION_REWRITE_ATTEMPTS = 4
VALIDATION_PAGE_FILENAME = "03-excel-parity-validation.qmd"
# Great Docs strips numeric prefixes when publishing user-guide pages, so the
# landing-page link must use the published slug rather than the source filename.
VALIDATION_PAGE_LINK = "user-guide/excel-parity-validation.qmd"
# Keep section-rewrite prompts well inside common context windows even when the
# source Functional Overview section is large. Measured prompts with
# signature-only API context land far below this ceiling.
MAX_SECTION_REWRITE_PROMPT_CHARS = 350_000
REWRITE_API_EXCLUDE_PREFIXES: tuple[str, ...] = ("list_",)
SECTION_REWRITE_REQUEST_TIMEOUT_ENV = "SECTION_REWRITE_REQUEST_TIMEOUT"
DEFAULT_SECTION_REWRITE_REQUEST_TIMEOUT = 300.0
SECTION_REWRITE_DEADLINE_ENV = "SECTION_REWRITE_DEADLINE"

SETTER_INPUT_SHAPE_GUIDANCE = (
    "Setter input shapes: single-cell setters accept a bare scalar (not a "
    "one-element list). Series setters accept a 1-D sequence of measure values, "
    "a tidy Polars DataFrame, a single record, or a list of records when the "
    "series is keyed. Positional series input must supply exactly one measure "
    "per key in that setter's canonical key order — match the full positional "
    "length for the series, or prefer keyed records / a single record when "
    "updating only some keys. Never wrap a single scenario scalar in a "
    "one-element list for a multi-key series setter. Do not call a profile-table "
    "series setter merely to set the selected entity's value; use the scalar "
    "selector (for example set_example_selector) unless the example is "
    "intentionally rewriting the table. Reuse ctx = make_context() across "
    "runnable cells. Tabulate compute_* results with Polars, selecting the "
    "measure column with a clear alias as shown in the canonical_api_usage "
    "reference."
)


def resolve_section_rewrite_request_timeout() -> float:
    return (
        env_float(SECTION_REWRITE_REQUEST_TIMEOUT_ENV)
        or DEFAULT_SECTION_REWRITE_REQUEST_TIMEOUT
    )


def resolve_section_rewrite_deadline_seconds() -> float:
    """Wall-clock budget for one section rewrite retry loop."""
    override = env_float(SECTION_REWRITE_DEADLINE_ENV)
    if override is not None:
        return override
    return resolve_section_rewrite_request_timeout() * MAX_SECTION_REWRITE_ATTEMPTS


def _rewrite_cache_path(config: PipelineConfig) -> Path:
    return config.repo_root / ".cache" / "guide-rewrites.json"


def _user_guide_root(config: PipelineConfig) -> Path:
    return config.dist_root / "user_guide"


def _great_docs_yml(config: PipelineConfig) -> Path:
    return config.dist_root / "great-docs.yml"


def _docs_workflow_path(config: PipelineConfig) -> Path:
    return config.dist_root / ".github" / "workflows" / "deploy-docs.yml"


def _no_api_signatures() -> str:
    return "No generated package API symbols are required for this section."


def _format_section_focus_template(template_path: Path, **placeholders: str) -> str:
    return template_path.read_text(encoding="utf-8").strip().format(**placeholders)


def introduction_focus_instructions(config: PipelineConfig) -> str:
    metadata = config.dist_metadata
    install = metadata.resolved_install_command()
    repo_hint = (
        f"from {metadata.repository_url}"
        if metadata.repository_url
        else "from the configured package source"
    )
    return _format_section_focus_template(
        config.section_rewrite_introduction_focus_path,
        install=install,
        repo_hint=repo_hint,
        library_name=metadata.library_name,
    )


_INSTALL_FENCE_COMMAND = re.compile(
    r"(```(?:bash|sh|shell|zsh)?\n)"
    r"((?:uv add|python -m pip install|pip install)[^\n]+)"
    r"(\n```)"
)


def ensure_introduction_install_recommendation(
    markdown: str, *, install_command: str
) -> str:
    """Guarantee the landing page recommends the configured install command."""
    if install_command in markdown:
        return markdown

    if _INSTALL_FENCE_COMMAND.search(markdown):
        return _INSTALL_FENCE_COMMAND.sub(
            rf"\g<1>{install_command}\g<3>",
            markdown,
            count=1,
        )

    section = (
        "### Installation\n\n"
        "Install the package directly from the GitHub repository:\n\n"
        f"```bash\n{install_command}\n```\n"
    )
    getting_started = re.search(r"^### Getting started\s*$", markdown, re.MULTILINE)
    if getting_started is not None:
        idx = getting_started.start()
        return f"{markdown[:idx]}{section}\n{markdown[idx:]}"
    return f"{markdown.rstrip()}\n\n{section}"


def functional_overview_focus_instructions(config: PipelineConfig) -> str:
    return _format_section_focus_template(
        config.section_rewrite_functional_overview_focus_path,
        api_import_path=config.api_import_path,
        canonical_api_example_path=config.repo_relative_posix_path(
            config.canonical_api_example_path
        ),
    )


def illustrative_example_focus_instructions(config: PipelineConfig) -> str:
    return _format_section_focus_template(
        config.section_rewrite_illustrative_example_focus_path,
        api_import_path=config.api_import_path,
        canonical_api_example_path=config.repo_relative_posix_path(
            config.canonical_api_example_path
        ),
    )


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
        description="Symbols from the generated package API referenced in the rewritten section."
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


def render_validation_page(
    summary: ParityReportSummary,
    *,
    library_name: str,
    package_name: str,
    evidence_kind: str = "exported_library",
) -> str:
    """Render the deterministic GreatDocs page for Excel parity validation."""
    passed = f"{summary.passed:,}"
    total = f"{summary.total_comparisons:,}"
    failed = f"{summary.failed:,}"
    if evidence_kind == "dependency_graph":
        what_tested = (
            f"The current reference evidence is **dependency-graph parity**: the "
            f"extracted `{package_name}` evaluation graph was compared against "
            f"Microsoft Excel through `xlwings` across the configured scenario "
            f"sweep. Exported-library reference reports were not present, so this "
            f"page summarizes the graph-oracle result until those reports are "
            f"refreshed on Windows."
        )
        report_path = "`data/differential/graph/differential_report.txt`"
        harness = "`tests/differential/differential_test_graph.py`"
    else:
        what_tested = (
            f"The validation checks the exported standalone library, not just the "
            f"extraction graph. It imports `{package_name}.api`, creates a fresh "
            f"context for each scenario, sets inputs through the records-shaped "
            f"public setters, computes the exported output series, and compares "
            f"those values against the workbook's calculated output cells."
        )
        report_path = "`tests/results/reference/parity_report.txt`"
        harness = "`tests/differential/differential_test_exported_library.py`"
    return f"""---
title: "Excel parity validation"
---

{library_name} includes an exported validation bundle that checks the generated
`{package_name}` package against the source Excel workbook. The test drives
the workbook with Microsoft Excel through `xlwings`, applies the same inputs
through the package's public `set_*` functions, and compares calculated outputs
cell by cell.

## Current reference result

The current reference run is **{summary.result}**: **{passed} / {total}**
cell-level comparisons passed at `{summary.tolerance}`.

- Result: **{summary.result}**
- Generated: `{summary.generated}`
- Passed: **{passed}**
- Failed: **{failed}**
- Pass rate: **{summary.pass_rate}**
- Acceptance bar: **{summary.acceptance_bar}**
- Evidence: **{evidence_kind}**

The sweep covers **{total}** cell-level comparisons against Excel.

## What Was Tested

{what_tested}

## Inspect Or Re-run

The validation bundle is shipped in the source repository under `tests/`.
Because the golden-master oracle uses Microsoft Excel through COM automation,
reruns require Windows with Microsoft Excel installed.

- Reference parity report: {report_path}
- Validation bundle README: `tests/README.md`
- Differential test harness: {harness}

To re-run exported-library validation from the exported project:

```pwsh
uv run --project . --group validation python -m tests.differential.differential_test_exported_library --layout exported
```
"""


def render_introduction_validation_note() -> str:
    """Return a short deterministic landing-page pointer to validation evidence."""
    return (
        "For correctness evidence, see "
        f"[Excel parity validation]({VALIDATION_PAGE_LINK}), which summarizes "
        "the exported-library differential test against the source workbook."
    )


def write_validation_page(*, config: PipelineConfig) -> None:
    """Write the deterministic user-guide page from exported validation assets."""
    user_guide_root = _user_guide_root(config)
    report_path = (
        config.dist_root / "tests" / "results" / "reference" / "parity_report.txt"
    )
    readme_path = config.dist_root / "tests" / "README.md"
    if not readme_path.is_file():
        raise FileNotFoundError(f"Validation README not found: {readme_path}")

    evidence_kind = "exported_library"
    if report_path.is_file():
        report_text = report_path.read_text(encoding="utf-8")
    else:
        graph_report = (
            config.repo_root
            / config.differential_graph_report_dir_rel
            / "differential_report.txt"
        )
        if not graph_report.is_file():
            raise FileNotFoundError(
                f"Reference parity report not found: {report_path} "
                f"(graph fallback also missing: {graph_report})"
            )
        report_text = graph_report.read_text(encoding="utf-8")
        evidence_kind = "dependency_graph"

    summary = parse_parity_report(report_text)
    user_guide_root.mkdir(parents=True, exist_ok=True)
    (user_guide_root / VALIDATION_PAGE_FILENAME).write_text(
        render_validation_page(
            summary,
            library_name=config.dist_metadata.library_name,
            package_name=config.dist_metadata.package_name,
            evidence_kind=evidence_kind,
        ),
        encoding="utf-8",
    )


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def load_rewrite_cache(config: PipelineConfig) -> dict[str, str]:
    cache_path = _rewrite_cache_path(config)
    if not cache_path.exists():
        return {}
    return json.loads(cache_path.read_text(encoding="utf-8"))


def save_rewrite_cache(config: PipelineConfig, cache: dict[str, str]) -> None:
    cache_path = _rewrite_cache_path(config)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def extract_markdown_section(markdown_text: str, heading: str) -> str:
    """Return the body under a ``##`` heading.

    Optional Markdown footnote markers after the heading (for example
    ``[^1]``) are accepted so guide extracts stay stable across PDF-derived
    heading variants.
    """
    pattern = rf"^## {re.escape(heading)}(?:\[\^[^\]]+\])?\n(.*?)(?=^## |\Z)"
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


def load_canonical_api_example(config: PipelineConfig) -> str:
    return (
        config.canonical_api_example_path.read_text(encoding="utf-8")
        .strip()
        .format(api_import_path=config.api_import_path)
    )


def canonical_api_context(config: PipelineConfig) -> dict[str, str]:
    return {
        "canonical_api_usage": load_canonical_api_example(config),
    }


def extract_api_signatures(
    api_path: Path,
    symbol_names: list[str],
    *,
    include_body: bool = True,
    exclude_prefixes: tuple[str, ...] = (),
) -> str:
    """Extract public API snippets for prompt context.

    When ``include_body`` is false, only the function signature and docstring are
    kept so large generated modules (and introspection helpers that return giant
    literals) cannot dominate section-rewrite prompts.
    """
    source = api_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    blocks: list[str] = []
    lines = source.splitlines()
    wanted = set(symbol_names)
    seen: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
            continue
        if any(node.name.startswith(prefix) for prefix in exclude_prefixes):
            continue
        if node.name in seen:
            continue
        seen.add(node.name)
        start = node.lineno - 1
        if include_body:
            end = node.end_lineno
        elif (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            end = node.body[0].end_lineno
        else:
            end = node.lineno
        if end is None:
            continue
        blocks.append("\n".join(lines[start:end]))
    if not blocks:
        raise ValueError(f"No signatures found for symbols: {symbol_names}")
    return "\n\n".join(blocks)


def rewrite_api_signatures(api_path: Path, symbol_names: list[str]) -> str:
    """API context for guide rewrites: signatures + docstrings, no bodies."""
    return extract_api_signatures(
        api_path,
        symbol_names,
        include_body=False,
        exclude_prefixes=REWRITE_API_EXCLUDE_PREFIXES,
    )


def estimate_prompt_chars(prompt: str) -> int:
    return len(prompt)


def validate_section_rewrite_prompt_budget(prompt: str) -> None:
    """Fail loudly when a section-rewrite prompt exceeds the configured budget."""
    size = estimate_prompt_chars(prompt)
    if size > MAX_SECTION_REWRITE_PROMPT_CHARS:
        raise ValueError(
            f"section rewrite prompt budget exceeded: {size:,} chars > "
            f"{MAX_SECTION_REWRITE_PROMPT_CHARS:,} chars. Slim API context or "
            "chunk the source section before calling the model."
        )


def build_section_prompt(
    *,
    library_name: str,
    api_import_path: str,
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
Rewrite one section from the workbook guide into Python-first documentation for {library_name}.

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
- {SETTER_INPUT_SHAPE_GUIDANCE}
- Use matplotlib when plots are needed.

Section name: {section_name}

Source section:
{source_section_markdown}

Python focus instructions:
{python_focus_instructions}

{api_import_path} signatures:
{api_signatures}

Response schema:
{json.dumps(response_schema, indent=2)}
""".strip()


def section_rewrite_model() -> str:
    return model_from_env(SECTION_REWRITE_MODEL_ENV)


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
        "model": section_rewrite_model(),
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
    config: PipelineConfig,
    guide_text: str,
) -> None:
    """Update cached guide rewrites from validated generated QMD pages."""
    response_schema = SectionRewriteResponse.model_json_schema()
    api_symbols = list(discover_public_api_symbols(config.api_module_path))
    api_context = canonical_api_context(config)
    user_guide_root = _user_guide_root(config)
    cache_path = _rewrite_cache_path(config)

    rewrite_signatures = rewrite_api_signatures(config.api_module_path, api_symbols)
    functional_key = rewrite_cache_key(
        section_id="functional_overview",
        source_section_markdown=extract_markdown_section(
            guide_text, "II. Functional Overview"
        ),
        python_focus_instructions=functional_overview_focus_instructions(config),
        pipeline_context_blocks=api_context,
        api_signatures=rewrite_signatures,
        response_schema=response_schema,
    )
    illustrative_key = rewrite_cache_key(
        section_id="illustrative_example",
        source_section_markdown=extract_markdown_section(
            guide_text, "III. Illustrative Example"
        ),
        python_focus_instructions=illustrative_example_focus_instructions(config),
        pipeline_context_blocks=api_context,
        api_signatures=rewrite_signatures,
        response_schema=response_schema,
    )

    sync_cached_rewrite_from_qmd(
        cache_path=cache_path,
        cache_key=functional_key,
        qmd_path=user_guide_root / "01-functional-overview.qmd",
    )
    sync_cached_rewrite_from_qmd(
        cache_path=cache_path,
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


def validate_rewritten_runnable_api_usage(
    markdown: str,
    *,
    rules: tuple[RunnableCellRule, ...] = (),
) -> None:
    """Reject runnable cells whose source matches a configured forbidden pattern.

    Docs may still describe fragile APIs in prose or non-executable
    `` ```python `` fences; only executable `` ```{python} `` cells are checked.
    Rules come from ``workbook_config.RUNNABLE_CELL_RULES``, so each derived
    repository can pin its own allowlist of runnable-safe APIs.
    """
    for cell in extract_python_cells(markdown):
        for rule in rules:
            if re.search(rule.pattern, cell.source):
                raise ValueError(
                    f"runnable cell matches forbidden pattern {rule.pattern!r}: "
                    f"{rule.message}"
                )


def _validate_section_rewrite(
    parsed: SectionRewriteResponse,
    *,
    runnable_cell_rules: tuple[RunnableCellRule, ...] = (),
) -> SectionRewriteResponse:
    validate_rewritten_markdown_fences(parsed.rewritten_markdown)
    validate_rewritten_runnable_api_usage(
        parsed.rewritten_markdown, rules=runnable_cell_rules
    )
    return parsed


def rewrite_guide_section(
    *,
    config: PipelineConfig,
    client: OpenAI | None,
    section_id: str,
    section_name: str,
    source_section_markdown: str,
    python_focus_instructions: str,
    pipeline_context_blocks: dict[str, str],
    api_signatures: str,
) -> SectionRewriteResponse:
    response_schema = SectionRewriteResponse.model_json_schema()
    cache = load_rewrite_cache(config)
    cache_key = rewrite_cache_key(
        section_id=section_id,
        source_section_markdown=source_section_markdown,
        python_focus_instructions=python_focus_instructions,
        pipeline_context_blocks=pipeline_context_blocks,
        api_signatures=api_signatures,
        response_schema=response_schema,
    )
    if cache_key in cache:
        print(f"guide_rewrite: cache hit for {section_id}", flush=True)
        return SectionRewriteResponse.model_validate_json(cache[cache_key])

    if client is None:
        provider = provider_for_model(section_rewrite_model())
        raise RuntimeError(
            f"{provider.api_key_env} is required to generate uncached guide rewrites"
        )

    api_import_path = config.api_import_path
    prompt = build_section_prompt(
        library_name=config.dist_metadata.library_name,
        api_import_path=api_import_path,
        section_name=section_name,
        source_section_markdown=source_section_markdown,
        python_focus_instructions=python_focus_instructions,
        pipeline_context_blocks=pipeline_context_blocks,
        api_signatures=api_signatures,
        response_schema=response_schema,
    )
    validate_section_rewrite_prompt_budget(prompt)

    def post_validate(parsed: SectionRewriteResponse) -> SectionRewriteResponse:
        return _validate_section_rewrite(
            parsed, runnable_cell_rules=config.runnable_cell_rules
        )

    model = section_rewrite_model()
    deadline_seconds = resolve_section_rewrite_deadline_seconds()
    print(
        f"guide_rewrite: generating {section_id} "
        f"(deadline={deadline_seconds:.0f}s, attempts<={MAX_SECTION_REWRITE_ATTEMPTS})",
        flush=True,
    )
    parsed, content = generate_validated_json(
        client=client,
        model=model,
        provider=provider_for_model(model),
        system_prompt=(
            f"You are a technical documentation writer for the "
            f"{config.dist_metadata.library_name} library. "
            f"Runnable examples use {api_import_path} with make_context(), "
            "setter input shapes from the reference example, and compute_* "
            "functions. Return only valid JSON matching the provided schema."
        ),
        user_prompt=prompt,
        response_model=SectionRewriteResponse,
        post_validate=post_validate,
        max_attempts=MAX_SECTION_REWRITE_ATTEMPTS,
        deadline_seconds=deadline_seconds,
    )
    cache[cache_key] = content
    save_rewrite_cache(config, cache)
    print(f"guide_rewrite: finished {section_id}", flush=True)
    return parsed


def has_top_level_key(yaml_content: str, key: str) -> bool:
    for line in yaml_content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}:"):
            return True
    return False


def configure_great_docs_yml(config: PipelineConfig) -> None:
    great_docs_yml = _great_docs_yml(config)
    package_module = config.dist_metadata.package_name
    content = great_docs_yml.read_text(encoding="utf-8")
    content = content.replace("# module: yaml12", f"module: {package_module}")

    great_docs_settings: list[tuple[str, str]] = [
        ("display_name", config.dist_metadata.library_name),
        ("homepage", "user_guide"),
        ("site_url", f'"{config.dist_metadata.documentation_url}"'),
    ]
    if config.dist_metadata.repository_url is not None:
        great_docs_settings.append(("repo", config.dist_metadata.repository_url))
    insert_lines = [
        f"{key}: {value}"
        for key, value in great_docs_settings
        if not has_top_level_key(content, key)
    ]
    if insert_lines:
        module_line = f"module: {package_module}"
        if module_line in content:
            content = content.replace(
                module_line,
                module_line + "\n" + "\n".join(insert_lines),
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
    input_text: str | None = None,
) -> None:
    command_env = os.environ.copy()
    if extra_env is not None:
        command_env.update(extra_env)
    # Force UTF-8 stdio for great-docs/Quarto children on Windows (cp1252/charmap).
    command_env["PYTHONIOENCODING"] = "utf-8"
    command_env["PYTHONUTF8"] = "1"
    subprocess.run(
        args,
        check=True,
        env=command_env,
        cwd=str(cwd) if cwd is not None else None,
        input=input_text,
        text=True if input_text is not None else None,
    )


def write_introduction_page(
    config: PipelineConfig,
    client: OpenAI | None,
    guide_text: str,
) -> None:
    user_guide_root = _user_guide_root(config)
    introduction_source = extract_markdown_section(guide_text, "I. Introduction")
    focus_instructions = introduction_focus_instructions(config)
    introduction_rewrite = rewrite_guide_section(
        config=config,
        client=client,
        section_id="introduction",
        section_name="Introduction",
        source_section_markdown=introduction_source,
        python_focus_instructions=focus_instructions,
        pipeline_context_blocks={},
        api_signatures=_no_api_signatures(),
    )
    install_command = config.dist_metadata.resolved_install_command()
    rewritten_markdown = ensure_introduction_install_recommendation(
        introduction_rewrite.rewritten_markdown,
        install_command=install_command,
    )
    if rewritten_markdown != introduction_rewrite.rewritten_markdown:
        introduction_rewrite = introduction_rewrite.model_copy(
            update={"rewritten_markdown": rewritten_markdown}
        )
        cache = load_rewrite_cache(config)
        cache_key = rewrite_cache_key(
            section_id="introduction",
            source_section_markdown=introduction_source,
            python_focus_instructions=focus_instructions,
            pipeline_context_blocks={},
            api_signatures=_no_api_signatures(),
            response_schema=SectionRewriteResponse.model_json_schema(),
        )
        cache[cache_key] = introduction_rewrite.model_dump_json(indent=2)
        save_rewrite_cache(config, cache)
    user_guide_root.mkdir(parents=True, exist_ok=True)
    landing_page_output = user_guide_root / "index.qmd"
    landing_page_qmd = f"""---
title: "{introduction_rewrite.title}"
---

{introduction_rewrite.rewritten_markdown}

{render_introduction_validation_note()}
"""
    landing_page_output.write_text(landing_page_qmd, encoding="utf-8")


def write_rewritten_guide_pages(config: PipelineConfig, client: OpenAI | None) -> None:
    guide_text = config.guide_path.read_text(encoding="utf-8")
    api_symbols = list(discover_public_api_symbols(config.api_module_path))
    api_context = canonical_api_context(config)

    rewrite_signatures = rewrite_api_signatures(config.api_module_path, api_symbols)

    functional_overview_source = extract_markdown_section(
        guide_text,
        "II. Functional Overview",
    )
    functional_overview_rewrite = rewrite_guide_section(
        config=config,
        client=client,
        section_id="functional_overview",
        section_name="Functional Overview",
        source_section_markdown=functional_overview_source,
        python_focus_instructions=functional_overview_focus_instructions(config),
        pipeline_context_blocks=api_context,
        api_signatures=rewrite_signatures,
    )

    illustrative_example_source = extract_markdown_section(
        guide_text,
        "III. Illustrative Example",
    )
    illustrative_example_rewrite = rewrite_guide_section(
        config=config,
        client=client,
        section_id="illustrative_example",
        section_name="Illustrative Example",
        source_section_markdown=illustrative_example_source,
        python_focus_instructions=illustrative_example_focus_instructions(config),
        pipeline_context_blocks=api_context,
        api_signatures=rewrite_signatures,
    )

    user_guide_root = _user_guide_root(config)
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


def write_docs_deploy_workflow(config: PipelineConfig) -> None:
    docs_workflow_path = _docs_workflow_path(config)
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


def _section_rewrite_client(model: str) -> OpenAI | None:
    """Build a docs-stage client with a longer timeout for large section rewrites."""
    provider = provider_for_model(model)
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        return None
    timeout = resolve_section_rewrite_request_timeout()
    # Validation retries are owned by generate_validated_json; disable SDK
    # retries so they cannot multiply the per-request timeout budget.
    return OpenAI(
        api_key=api_key,
        base_url=provider.base_url,
        timeout=timeout,
        max_retries=0,
    )


def run_documentation_pipeline(config: PipelineConfig) -> None:
    configure_logging()
    load_dotenv(config.repo_root / ".env")
    timer = StageTimer()
    with monitor_pipeline_stage(
        timer,
        "document",
        stall_log_path=resolve_stall_log_path(config.dist_root),
    ):
        _run_documentation_pipeline_body(config)


def _run_documentation_pipeline_body(config: PipelineConfig) -> None:
    great_docs_yml = _great_docs_yml(config)
    if not great_docs_yml.exists():
        # great-docs init may prompt to append great-docs/ to .gitignore; answer
        # non-interactively so unattended pipeline runs cannot stall on stdin.
        run_cmd(
            [
                "uv",
                "run",
                "--project",
                str(config.dist_root),
                "--with",
                "great-docs",
                "great-docs",
                "init",
                "--project-path",
                str(config.dist_root),
            ],
            input_text="y\n",
        )

    configure_great_docs_yml(config)

    model = section_rewrite_model()
    section_client = _section_rewrite_client(model)
    guide_text = config.guide_path.read_text(encoding="utf-8")
    write_introduction_page(config, section_client, guide_text)
    write_rewritten_guide_pages(config, section_client)
    write_validation_page(config=config)
    api_symbols = list(discover_public_api_symbols(config.api_module_path))
    validate_qmd_files(
        dist_root=config.dist_root,
        qmd_paths=sorted(_user_guide_root(config).glob("*.qmd")),
        api_policy=PublicApiPolicy(
            api_import_path=config.api_import_path,
            allowed_symbols=frozenset(api_symbols),
        ),
        metadata=config.dist_metadata,
        client=section_client,
        model=model if section_client is not None else None,
        api_signatures=rewrite_api_signatures(config.api_module_path, api_symbols),
    )
    sync_validated_pages_to_rewrite_cache(config=config, guide_text=guide_text)
    write_docs_deploy_workflow(config)
