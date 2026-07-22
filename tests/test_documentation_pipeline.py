import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from src.pipeline_config import (
    PipelineConfig,
    RunnableCellRule,
    discover_public_api_symbols,
    load_pipeline_config,
)
from src.documentation_pipeline import (
    MAX_SECTION_REWRITE_PROMPT_CHARS,
    SETTER_INPUT_SHAPE_GUIDANCE,
    build_section_prompt,
    ensure_introduction_install_recommendation,
    estimate_prompt_chars,
    extract_api_signatures,
    extract_markdown_section,
    introduction_focus_instructions,
    functional_overview_focus_instructions,
    illustrative_example_focus_instructions,
    load_canonical_api_example,
    parse_parity_report,
    render_validation_page,
    rewrite_api_signatures,
    run_cmd,
    validate_rewritten_markdown_fences,
    validate_rewritten_runnable_api_usage,
    validate_section_rewrite_prompt_budget,
)


def test_load_canonical_api_example_reads_template() -> None:
    config = load_pipeline_config()
    example = load_canonical_api_example(config)
    assert "make_context()" in example
    assert "compute_" in example
    assert "import polars as pl" in example
    assert f"from {config.api_import_path} import" in example
    assert "bare scalar" in example
    assert "full key order" in example or "full key-order" in example
    assert "keyed record" in example.lower()


def test_load_canonical_api_example_injects_api_import_path() -> None:
    config = load_pipeline_config()
    config = replace(
        config,
        dist_metadata=replace(config.dist_metadata, package_name="tiny_dsa"),
    )
    example = load_canonical_api_example(config)
    prompt = build_section_prompt(
        library_name=config.dist_metadata.library_name,
        api_import_path=config.api_import_path,
        section_name="Functional Overview",
        source_section_markdown="Source",
        python_focus_instructions="Mirror the canonical example.",
        pipeline_context_blocks={"canonical_api_usage": example},
        api_signatures="def make_context(): ...",
        response_schema={"type": "object"},
    )

    assert "from tiny_dsa.api import" in example
    assert "my_model.api" not in example
    assert "from tiny_dsa.api import" in prompt
    assert "my_model.api" not in prompt
    assert "```{python}" in example


def test_section_focus_templates_use_placeholders() -> None:
    config = load_pipeline_config()

    introduction = introduction_focus_instructions(config)
    assert config.dist_metadata.library_name in introduction
    assert config.dist_metadata.resolved_install_command() in introduction
    assert "Installation subsection" in introduction

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
    assert "exactly one measure per key" in prompt
    assert "one-element list" in prompt
    assert "profile-table" in prompt or "profile table" in prompt


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


def test_runnable_api_usage_rejects_configured_patterns() -> None:
    rules = (
        RunnableCellRule(
            pattern=r"\bcompute_fragile_\w+\s*\(",
            message="fragile computes belong in prose or ```python fences",
        ),
        RunnableCellRule(
            pattern=r"""\bset_entity\s*\([^)]*['"]Atlantis['"]""",
            message="use a real registry entity in executable cells",
        ),
    )
    fragile = (
        "```{python}\n"
        "from my_model.api import compute_fragile_output\n"
        "compute_fragile_output(ctx=ctx)\n"
        "```\n"
    )
    fictional = (
        "```{python}\n"
        "from my_model.api import set_entity\n"
        'set_entity(ctx, "Atlantis")\n'
        "```\n"
    )
    with pytest.raises(ValueError, match="fragile computes"):
        validate_rewritten_runnable_api_usage(fragile, rules=rules)
    with pytest.raises(ValueError, match="real registry entity"):
        validate_rewritten_runnable_api_usage(fictional, rules=rules)


def test_runnable_api_usage_allows_matches_in_non_executable_fences() -> None:
    rules = (
        RunnableCellRule(
            pattern=r"\bcompute_fragile_\w+\s*\(",
            message="fragile computes belong in prose",
        ),
    )
    markdown = (
        "```python\n"
        "from my_model.api import compute_fragile_output\n"
        "compute_fragile_output(ctx=ctx)\n"
        "```\n"
    )
    validate_rewritten_runnable_api_usage(markdown, rules=rules)


def test_runnable_api_usage_allows_clean_cells_and_defaults_to_no_rules() -> None:
    markdown = (
        "```{python}\n"
        "from my_model.api import make_context, compute_example_output\n"
        "ctx = make_context()\n"
        "compute_example_output(ctx=ctx)\n"
        "```\n"
    )
    validate_rewritten_runnable_api_usage(markdown)
    validate_rewritten_runnable_api_usage(
        markdown,
        rules=(
            RunnableCellRule(
                pattern=r"\bcompute_fragile_\w+\s*\(",
                message="fragile computes belong in prose",
            ),
        ),
    )


def test_extract_markdown_section_allows_optional_footnote_suffix() -> None:
    markdown = "## I. Introduction\n\nHello.\n\n## II. Functional Overview\n\nBody.\n"
    assert extract_markdown_section(markdown, "I. Introduction") == "Hello."
    assert (
        extract_markdown_section(
            "## I. Introduction[^1]\n\nHello.\n\n## II. Next\n\nX\n",
            "I. Introduction",
        )
        == "Hello."
    )


def test_discover_public_api_symbols_deduplicates_redefined_names(
    tmp_path: Path,
) -> None:
    api_path = tmp_path / "api.py"
    api_path.write_text(
        "def compute_scenario_output():\n    return []\n\n"
        "def compute_scenario_output():\n    return [1]\n\n"
        "def make_context():\n    return {}\n",
        encoding="utf-8",
    )
    assert discover_public_api_symbols(api_path) == (
        "compute_scenario_output",
        "make_context",
    )


def test_extract_api_signatures_can_omit_function_bodies(tmp_path: Path) -> None:
    api_path = tmp_path / "api.py"
    api_path.write_text(
        '''
def make_context():
    """Create context."""
    return {"huge": "payload" * 100}

def list_reader_leaves():
    """Introspection helper."""
    return ["leaf"] * 1000

def set_selector(ctx, value):
    """Select the entity."""
    ctx["selector"] = value
'''.strip()
        + "\n",
        encoding="utf-8",
    )
    full = extract_api_signatures(api_path, ["make_context", "set_selector"])
    assert 'return {"huge"' in full

    slim = extract_api_signatures(
        api_path,
        ["make_context", "set_selector", "list_reader_leaves"],
        include_body=False,
        exclude_prefixes=("list_",),
    )
    assert "def make_context():" in slim
    assert '"""Create context."""' in slim
    assert "payload" not in slim
    assert "list_reader_leaves" not in slim
    assert "def set_selector" in slim


def test_rewrite_api_signatures_excludes_list_helpers_and_bodies(
    tmp_path: Path,
) -> None:
    api_path = tmp_path / "api.py"
    api_path.write_text(
        "def make_context():\n"
        '    """Create context."""\n'
        "    return {}\n\n"
        "def list_reader_leaves():\n"
        '    """Introspection helper."""\n'
        '    return ["leaf"] * 1000\n',
        encoding="utf-8",
    )
    signatures = rewrite_api_signatures(
        api_path, ["make_context", "list_reader_leaves"]
    )
    assert "def make_context():" in signatures
    assert "list_reader_leaves" not in signatures
    assert "return {}" not in signatures


def test_validate_section_rewrite_prompt_budget_rejects_huge_prompts() -> None:
    with pytest.raises(ValueError, match="prompt budget"):
        validate_section_rewrite_prompt_budget(
            "x" * (MAX_SECTION_REWRITE_PROMPT_CHARS + 1)
        )
    validate_section_rewrite_prompt_budget("x" * 100)
    assert estimate_prompt_chars("abc") == 3


def test_ensure_introduction_install_recommendation_is_idempotent() -> None:
    install = 'uv add "my-model @ git+https://github.com/example/my-model"'
    markdown = f"Intro.\n\n```bash\n{install}\n```\n"
    assert (
        ensure_introduction_install_recommendation(markdown, install_command=install)
        == markdown
    )


def test_ensure_introduction_install_recommendation_replaces_stale_command() -> None:
    install = 'uv add "my-model @ git+https://github.com/example/my-model"'
    markdown = (
        "### Installation\n\n"
        "```bash\n"
        'uv add "my-model @ git+https://github.com/example/extraction-pipeline"\n'
        "```\n\n"
        "### Getting started\n\n"
        "Next.\n"
    )
    updated = ensure_introduction_install_recommendation(
        markdown, install_command=install
    )
    assert install in updated
    assert "extraction-pipeline" not in updated


def test_ensure_introduction_install_recommendation_inserts_missing_section() -> None:
    install = 'uv add "my-model @ git+https://github.com/example/my-model"'
    markdown = "Provenance paragraph.\n\n### Getting started\n\nNext.\n"
    updated = ensure_introduction_install_recommendation(
        markdown, install_command=install
    )
    assert "### Installation" in updated
    assert install in updated
    assert updated.index("### Installation") < updated.index("### Getting started")


def test_run_cmd_forces_utf8_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import documentation_pipeline as docs

    run_kwargs: dict[str, object] = {}

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        run_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(docs.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

    run_cmd(
        ["python", "-c", "pass"],
        extra_env={
            "PYTHONUTF8": "0",
            "PYTHONIOENCODING": "cp1252",
            "MARKER": "1",
        },
    )

    env = cast(dict[str, str], run_kwargs["env"])
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["MARKER"] == "1"


def test_configure_great_docs_yml_sets_repo_and_site_url(tmp_path: Path) -> None:
    from src.documentation_pipeline import configure_great_docs_yml

    config = load_pipeline_config()
    config = replace(
        config,
        dist_root=tmp_path,
        dist_metadata=replace(
            config.dist_metadata,
            repository_url="https://github.com/example/my-model",
        ),
    )
    yml = tmp_path / "great-docs.yml"
    yml.write_text("module: my_model\n", encoding="utf-8")

    configure_great_docs_yml(config)

    content = yml.read_text(encoding="utf-8")
    assert f'site_url: "{config.dist_metadata.documentation_url}"' in content
    assert "repo: https://github.com/example/my-model" in content
    assert f"display_name: {config.dist_metadata.library_name}" in content
    assert "homepage: user_guide" in content


def test_configure_great_docs_yml_omits_repo_when_unset(tmp_path: Path) -> None:
    from src.documentation_pipeline import configure_great_docs_yml

    config = load_pipeline_config()
    config = replace(
        config,
        dist_root=tmp_path,
        dist_metadata=replace(config.dist_metadata, repository_url=None),
    )
    yml = tmp_path / "great-docs.yml"
    yml.write_text("module: my_model\n", encoding="utf-8")

    configure_great_docs_yml(config)

    content = yml.read_text(encoding="utf-8")
    assert f'site_url: "{config.dist_metadata.documentation_url}"' in content
    assert "repo:" not in content


def test_write_validation_page_falls_back_to_graph_report(tmp_path: Path) -> None:
    from src import documentation_pipeline as doc
    from src.pipeline_config import DistProjectMetadata

    dist = tmp_path / "dist"
    tests_root = dist / "tests"
    tests_root.mkdir(parents=True)
    (tests_root / "README.md").write_text("validation", encoding="utf-8")
    graph_report = (
        tmp_path / "data" / "differential" / "graph" / "differential_report.txt"
    )
    graph_report.parent.mkdir(parents=True)
    graph_report.write_text(
        """Generated: 2026-01-01
Tolerance: 1e-6
Total comparisons: 10
Passed: 10
Failed: 0
Pass rate: 100%
Acceptance bar: 100%
Result: PASS
""",
        encoding="utf-8",
    )
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in (
        "canonical-api-usage.md",
        "binding-authoring-prompt.txt",
        "section-rewrite-introduction-focus.txt",
        "section-rewrite-functional-overview-focus.txt",
        "section-rewrite-illustrative-example-focus.txt",
    ):
        (templates / name).write_text("x", encoding="utf-8")

    config = PipelineConfig(
        repo_root=tmp_path,
        workbook_path=tmp_path / "data" / "workbook.xlsx",
        guide_path=tmp_path / "data" / "guide.md",
        bindings_path=tmp_path / "bindings",
        dist_root=dist,
        targets=(),
        constraints={},
        dist_metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="test",
            documentation_url="https://example.com/",
        ),
        docstring_callback_name="series_docs",
        projection_layout=None,
        canonical_api_example_path=templates / "canonical-api-usage.md",
        binding_authoring_prompt_path=templates / "binding-authoring-prompt.txt",
        section_rewrite_introduction_focus_path=(
            templates / "section-rewrite-introduction-focus.txt"
        ),
        section_rewrite_functional_overview_focus_path=(
            templates / "section-rewrite-functional-overview-focus.txt"
        ),
        section_rewrite_illustrative_example_focus_path=(
            templates / "section-rewrite-illustrative-example-focus.txt"
        ),
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=tmp_path / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
    )
    doc.write_validation_page(config=config)
    page = (dist / "user_guide" / "03-excel-parity-validation.qmd").read_text(
        encoding="utf-8"
    )
    assert "PASS" in page
    assert "dependency_graph" in page or "dependency-graph" in page.lower()


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
