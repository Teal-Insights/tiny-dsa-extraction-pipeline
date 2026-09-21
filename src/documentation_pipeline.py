"""Generate Great Docs content and CI workflow for exported docs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cursor_sdk import (
    AgentOptions,
    AsyncAgent,
    AsyncClient,
    CursorAgentError,
    LocalAgentOptions,
    RunResult,
)
from dotenv import load_dotenv

from src.env_utils import env_float
from src.logging_config import configure_logging
from src.pipeline_config import PipelineConfig, RunnableCellRule
from src.pipeline_monitor import (
    StageTimer,
    monitor_pipeline_stage,
    resolve_stall_log_path,
)
from src.qmd_python_validation import (
    DOCUMENTATION_BASELINE_DEV_DEPS,
    VALIDATION_BASELINE_DEV_DEPS,
    extract_python_cells,
    merge_dev_dependencies,
    parse_dev_dependencies_from_pyproject,
    write_dist_pyproject,
)

DOCUMENT_AGENT_MODEL_ENV = "DOCUMENT_AGENT_MODEL"
DEFAULT_DOCUMENT_AGENT_MODEL = "gpt-5.6-luna"
USER_GUIDE_AGENT_PROMPT_VERSION = 2
DOCUMENT_AGENT_DEADLINE_ENV = "DOCUMENT_AGENT_DEADLINE"
DEFAULT_DOCUMENT_AGENT_DEADLINE_SECONDS = 1800.0
CURSOR_API_KEY_ENV = "CURSOR_API_KEY"

GUIDANCE_NOTE_RELATIVE = Path("docs-source") / "guidance-note.md"
VALIDATION_PAGE_FILENAME = "99-excel-parity-validation.qmd"
# Great Docs strips numeric prefixes when publishing user-guide pages, so the
# landing-page link must use the published slug rather than the source filename.
VALIDATION_PAGE_LINK = "user-guide/excel-parity-validation.qmd"
PACKAGE_MODULE_NAMES = (
    "__init__.py",
    "api.py",
    "data.py",
    "runtime.py",
    "internals.py",
)
GREAT_DOCS_BUILD_COMMAND = (
    "uv run --project . --with great-docs great-docs build --project-path ."
)
ACTIVE_AGENT_FILENAME = "active.json"
EXTRA_DEPS_FILENAME = "extra-dev-deps.json"


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


def document_agent_model() -> str:
    return os.environ.get(DOCUMENT_AGENT_MODEL_ENV) or DEFAULT_DOCUMENT_AGENT_MODEL


def resolve_document_agent_deadline_seconds() -> float:
    override = env_float(DOCUMENT_AGENT_DEADLINE_ENV)
    if override is not None:
        return override
    return DEFAULT_DOCUMENT_AGENT_DEADLINE_SECONDS


def require_cursor_api_key() -> str:
    api_key = os.environ.get(CURSOR_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(
            f"{CURSOR_API_KEY_ENV} is required to generate uncached user-guide docs"
        )
    return api_key


def _user_guide_root(config: PipelineConfig) -> Path:
    return config.dist_root / "user_guide"


def _great_docs_yml(config: PipelineConfig) -> Path:
    return config.dist_root / "great-docs.yml"


def _docs_workflow_path(config: PipelineConfig) -> Path:
    return config.dist_root / ".github" / "workflows" / "deploy-docs.yml"


def _user_guide_cache_root(config: PipelineConfig) -> Path:
    return config.repo_root / ".cache" / "user-guide"


def _active_agent_path(config: PipelineConfig) -> Path:
    return _user_guide_cache_root(config) / ACTIVE_AGENT_FILENAME


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def build_user_guide_agent_prompt(config: PipelineConfig) -> str:
    template = config.user_guide_agent_prompt_path.read_text(encoding="utf-8")
    return template.format(
        library_name=config.dist_metadata.library_name,
        api_import_path=config.api_import_path,
        install_command=config.dist_metadata.resolved_install_command(),
        package_name=config.dist_metadata.package_name,
    ).strip()


def user_guide_cache_key(config: PipelineConfig) -> str:
    api_text = ""
    if config.api_module_path.is_file():
        api_text = config.api_module_path.read_text(encoding="utf-8")
    guide_text = ""
    if config.guide_path.is_file():
        guide_text = config.guide_path.read_text(encoding="utf-8")
    prompt_template = ""
    if config.user_guide_agent_prompt_path.is_file():
        prompt_template = config.user_guide_agent_prompt_path.read_text(
            encoding="utf-8"
        )
    payload = {
        "model": document_agent_model(),
        "prompt_version": USER_GUIDE_AGENT_PROMPT_VERSION,
        "prompt_template": prompt_template,
        "guidance_note": guide_text,
        "api_module": api_text,
        "library_name": config.dist_metadata.library_name,
        "package_name": config.dist_metadata.package_name,
        "api_import_path": config.api_import_path,
        "install_command": config.dist_metadata.resolved_install_command(),
        "documentation_url": config.dist_metadata.documentation_url,
        "repository_url": config.dist_metadata.repository_url,
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def copy_guidance_note(config: PipelineConfig) -> Path:
    destination = config.dist_root / GUIDANCE_NOTE_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.guide_path, destination)
    return destination


def snapshot_package_modules(config: PipelineConfig) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for name in PACKAGE_MODULE_NAMES:
        path = config.package_root / name
        if path.is_file():
            snapshot[name] = path.read_bytes()
    return snapshot


def assert_package_modules_unchanged(
    config: PipelineConfig, snapshot: dict[str, bytes]
) -> None:
    for name, expected in snapshot.items():
        path = config.package_root / name
        if not path.is_file():
            raise RuntimeError(
                f"Document agent removed generated package module {name}"
            )
        actual = path.read_bytes()
        if actual != expected:
            raise RuntimeError(
                f"Document agent modified generated package module {name}"
            )
    for name in PACKAGE_MODULE_NAMES:
        path = config.package_root / name
        if path.is_file() and name not in snapshot:
            raise RuntimeError(
                f"Document agent created unexpected package module {name}"
            )


def assert_user_guide_pages_exist(config: PipelineConfig) -> None:
    qmd_paths = sorted(_user_guide_root(config).glob("*.qmd"))
    authored = [path for path in qmd_paths if path.name != VALIDATION_PAGE_FILENAME]
    if not authored:
        raise RuntimeError(
            "Document agent finished without writing any user_guide/*.qmd pages"
        )


def validate_runnable_cell_rules(
    config: PipelineConfig,
    *,
    rules: tuple[RunnableCellRule, ...] | None = None,
) -> None:
    cell_rules = config.runnable_cell_rules if rules is None else rules
    if not cell_rules:
        return
    for qmd_path in sorted(_user_guide_root(config).glob("*.qmd")):
        if qmd_path.name == VALIDATION_PAGE_FILENAME:
            continue
        for cell in extract_python_cells(qmd_path.read_text(encoding="utf-8")):
            for rule in cell_rules:
                if re.search(rule.pattern, cell.source):
                    raise ValueError(
                        f"{qmd_path.name}: runnable cell matches forbidden pattern "
                        f"{rule.pattern!r}: {rule.message}"
                    )


def merge_agent_pyproject_extras(config: PipelineConfig) -> list[str]:
    """Rewrite dist pyproject, preserving agent-added documentation deps."""
    pyproject_path = config.dist_root / "pyproject.toml"
    discovered: list[str] = []
    if pyproject_path.is_file():
        discovered = parse_dev_dependencies_from_pyproject(
            pyproject_path.read_text(encoding="utf-8")
        )
    extras = [dep for dep in discovered if dep not in DOCUMENTATION_BASELINE_DEV_DEPS]
    write_dist_pyproject(
        config.dist_root,
        dev_dependencies=merge_dev_dependencies(
            DOCUMENTATION_BASELINE_DEV_DEPS,
            extras,
        ),
        validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
        metadata=config.dist_metadata,
    )
    return extras


def save_user_guide_cache(
    config: PipelineConfig,
    *,
    cache_key: str,
    extra_deps: list[str],
) -> None:
    cache_dir = _user_guide_cache_root(config) / cache_key
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    user_guide = _user_guide_root(config)
    if user_guide.is_dir():
        shutil.copytree(
            user_guide,
            cache_dir / "user_guide",
            ignore=shutil.ignore_patterns(VALIDATION_PAGE_FILENAME),
        )
    (cache_dir / EXTRA_DEPS_FILENAME).write_text(
        json.dumps(extra_deps, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def restore_user_guide_cache(config: PipelineConfig, *, cache_key: str) -> bool:
    cache_dir = _user_guide_cache_root(config) / cache_key
    cached_guide = cache_dir / "user_guide"
    if not cached_guide.is_dir():
        return False
    user_guide = _user_guide_root(config)
    if user_guide.exists():
        shutil.rmtree(user_guide)
    shutil.copytree(cached_guide, user_guide)
    extras_path = cache_dir / EXTRA_DEPS_FILENAME
    extras: list[str] = []
    if extras_path.is_file():
        extras = list(json.loads(extras_path.read_text(encoding="utf-8")))
    write_dist_pyproject(
        config.dist_root,
        dev_dependencies=merge_dev_dependencies(
            DOCUMENTATION_BASELINE_DEV_DEPS,
            extras,
        ),
        validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
        metadata=config.dist_metadata,
    )
    return True


def clear_active_agent(config: PipelineConfig) -> None:
    path = _active_agent_path(config)
    if path.is_file():
        path.unlink()


def write_active_agent(
    config: PipelineConfig, *, agent_id: str, cache_key: str
) -> None:
    path = _active_agent_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"agent_id": agent_id, "cache_key": cache_key}, indent=2),
        encoding="utf-8",
    )


def load_active_agent(config: PipelineConfig) -> dict[str, str] | None:
    path = _active_agent_path(config)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    agent_id = payload.get("agent_id")
    cache_key = payload.get("cache_key")
    if not isinstance(agent_id, str) or not isinstance(cache_key, str):
        return None
    return {"agent_id": agent_id, "cache_key": cache_key}


def run_great_docs_build(config: PipelineConfig) -> None:
    run_cmd(
        [
            "uv",
            "run",
            "--project",
            str(config.dist_root),
            "--with",
            "great-docs",
            "great-docs",
            "build",
            "--project-path",
            str(config.dist_root),
        ],
        cwd=config.dist_root,
    )


async def _run_agent_with_deadline_async(
    *,
    agent: AsyncAgent,
    prompt: str,
    deadline_seconds: float,
) -> RunResult:
    """Send one prompt and wait for completion within ``deadline_seconds``.

    Do not concurrently iterate ``run.messages()`` / ``run.stream()`` while
    awaiting ``run.wait()``: AsyncRun shares one event generator, and dual
    consumers raise ``RuntimeError: anext(): asynchronous generator is already
    running``.
    """
    run = await agent.send(prompt)
    print(f"document_agent: run_id={run.id}", flush=True)

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(run.wait(), timeout=deadline_seconds)
    except TimeoutError:
        if run.supports("cancel"):
            await run.cancel()
        raise TimeoutError(
            f"Document agent exceeded deadline of {deadline_seconds:.0f}s "
            f"(run_id={run.id})"
        ) from None

    elapsed = time.monotonic() - started
    print(f"document_agent: finished in {elapsed:.1f}s", flush=True)
    return result


async def _run_user_guide_agent_async(
    config: PipelineConfig,
    *,
    cache_key: str,
    api_key: str,
) -> None:
    """Async body: use AsyncClient.launch_bridge (Windows-safe discovery).

    Sync ``Agent.create`` / ``Bridge.launch`` reads bridge stderr via
    ``selectors.select`` on a pipe handle, which raises WinError 10038 on
    native Windows. The async launcher reads stderr through asyncio streams.
    """
    model = document_agent_model()
    prompt = build_user_guide_agent_prompt(config)
    deadline_seconds = resolve_document_agent_deadline_seconds()
    workspace = str(config.dist_root.resolve())
    local_options = LocalAgentOptions(cwd=workspace)

    active = load_active_agent(config)
    async with await AsyncClient.launch_bridge(workspace=workspace) as client:
        if active is not None and active["cache_key"] == cache_key:
            print(
                f"document_agent: resuming agent_id={active['agent_id']}",
                flush=True,
            )
            # Resume takes a single options object; nest LocalAgentOptions via
            # AgentOptions so options_to_json can serialize them (a plain dict
            # with a LocalAgentOptions value is not JSON-serializable).
            agent = await client.agents.resume(
                active["agent_id"],
                AgentOptions(
                    model=model,
                    api_key=api_key,
                    local=local_options,
                ),
            )
        else:
            agent = await client.agents.create(
                model=model,
                api_key=api_key,
                local=local_options,
            )

        async with agent:
            print(f"document_agent: agent_id={agent.agent_id}", flush=True)
            if agent.agent_id:
                write_active_agent(config, agent_id=agent.agent_id, cache_key=cache_key)
            try:
                result = await _run_agent_with_deadline_async(
                    agent=agent,
                    prompt=prompt,
                    deadline_seconds=deadline_seconds,
                )
            except CursorAgentError as error:
                raise RuntimeError(
                    f"Document agent failed to start: {error.message} "
                    f"(retryable={error.is_retryable})"
                ) from error
            if result.status != "finished":
                raise RuntimeError(
                    f"Document agent run failed with status={result.status!r} "
                    f"(run_id={result.id})"
                )

    # active.json is cleared only after a finished run so failed/interrupted
    # attempts can resume the same agent_id for this cache key.
    clear_active_agent(config)


def run_user_guide_agent(
    config: PipelineConfig,
    *,
    cache_key: str,
    api_key: str | None = None,
) -> None:
    """Launch or resume a local Cursor agent to author ``user_guide/`` pages."""
    key = api_key if api_key is not None else require_cursor_api_key()
    asyncio.run(_run_user_guide_agent_async(config, cache_key=cache_key, api_key=key))


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
    if evidence_kind == "formula_evaluator":
        what_tested = (
            f"The validation compares `{package_name}` `compute_*` results against "
            f"excel-grapher's FormulaEvaluator on the extraction graph, using the "
            f"workbook's default scenario. It does not drive Microsoft Excel or "
            f"public `set_*` setters."
        )
        report_path = "`tests/results/reference/parity_report.txt`"
        harness = "the pipeline `validate` stage (`FormulaEvaluator`)"
        intro = (
            f"{library_name} includes a FormulaEvaluator parity check for the "
            f"generated `{package_name}` package. The test evaluates the extraction "
            f"graph and compares those cells to keyword-only `compute_*` results "
            f"from the exported library."
        )
        sweep = (
            f"The current run covers **{total}** cell-level comparisons against "
            "the FormulaEvaluator."
        )
        rerun_note = (
            "Rerun the pipeline `validate` stage after export and annotate. "
            "Microsoft Excel is not required."
        )
        rerun_block = (
            "To re-run FormulaEvaluator parity, run the extraction pipeline "
            "through the `validate` stage."
        )
    elif evidence_kind == "dependency_graph":
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
        intro = (
            f"{library_name} includes an exported validation bundle that checks the "
            f"generated `{package_name}` package against the source Excel workbook. "
            f"The test drives the workbook with Microsoft Excel through `xlwings`, "
            f"applies the same inputs through the package's public `set_*` functions, "
            f"and compares calculated outputs cell by cell."
        )
        sweep = f"The sweep covers **{total}** cell-level comparisons against Excel."
        rerun_note = (
            "Because the golden-master oracle uses Microsoft Excel through COM "
            "automation, reruns require Windows with Microsoft Excel installed."
        )
        rerun_block = """To re-run exported-library validation from the exported project:

```pwsh
uv run --project . --group validation python -m tests.differential.differential_test_exported_library --layout exported
```"""
    else:
        what_tested = (
            f"The validation checks the exported standalone library, not just the "
            f"extraction graph. It imports `{package_name}.api`, calls keyword-only "
            f"`compute_*` for each scenario, and compares those values against "
            f"excel-grapher's FormulaEvaluator on the same input cells."
        )
        report_path = "`tests/results/reference/parity_report.txt`"
        harness = "`tests/differential/differential_test_exported_library.py`"
        intro = (
            f"{library_name} includes an exported validation bundle that checks the "
            f"generated `{package_name}` package against the extraction graph. "
            f"The test evaluates the graph with FormulaEvaluator, applies the same "
            f"inputs through keyword-only `compute_*` functions, and compares "
            f"outputs cell by cell."
        )
        sweep = (
            f"The sweep covers **{total}** cell-level comparisons against the "
            f"FormulaEvaluator."
        )
        rerun_note = (
            "The golden-master oracle is FormulaEvaluator; Microsoft Excel is not "
            "required. Run the sweep from the extraction repository after export."
        )
        rerun_block = """To re-run exported-library validation from the extraction repo:

```pwsh
uv run python -m tests.differential.differential_test_exported_library
```"""
    return f"""---
title: "Excel parity validation"
---

{intro}

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

{sweep}

## What Was Tested

{what_tested}

## Inspect Or Re-run

The validation bundle is shipped in the source repository under `tests/`.
{rerun_note}

- Reference parity report: {report_path}
- Validation bundle README: `tests/README.md`
- Differential test harness: {harness}

{rerun_block}
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

    if "FormulaEvaluator" in report_text:
        evidence_kind = "formula_evaluator"

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


def author_or_restore_user_guide(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
    """Restore cached user-guide pages or run the Cursor agent to author them."""
    cache_key = user_guide_cache_key(config)
    use_cache = not no_cache and not force_rebuild
    if use_cache and restore_user_guide_cache(config, cache_key=cache_key):
        print(f"document_agent: cache hit for {cache_key[:12]}", flush=True)
        return

    print(f"document_agent: cache miss for {cache_key[:12]}", flush=True)
    copy_guidance_note(config)
    snapshot = snapshot_package_modules(config)
    run_user_guide_agent(config, cache_key=cache_key)
    assert_package_modules_unchanged(config, snapshot)
    assert_user_guide_pages_exist(config)
    extras = merge_agent_pyproject_extras(config)
    save_user_guide_cache(config, cache_key=cache_key, extra_deps=extras)


def run_documentation_pipeline(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
    configure_logging()
    load_dotenv(config.repo_root / ".env")
    timer = StageTimer()
    with monitor_pipeline_stage(
        timer,
        "document",
        stall_log_path=resolve_stall_log_path(config.dist_root),
    ):
        _run_documentation_pipeline_body(
            config,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )


def _run_documentation_pipeline_body(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
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
    author_or_restore_user_guide(
        config,
        no_cache=no_cache,
        force_rebuild=force_rebuild,
    )
    write_validation_page(config=config)
    validate_runnable_cell_rules(config)
    run_great_docs_build(config)
    write_docs_deploy_workflow(config)
