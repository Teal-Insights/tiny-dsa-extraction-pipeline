"""Tests for agentic user-guide documentation pipeline."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.documentation_pipeline import (
    CURSOR_API_KEY_ENV,
    DEFAULT_DOCUMENT_AGENT_DEADLINE_SECONDS,
    DEFAULT_DOCUMENT_AGENT_MODEL,
    DOCUMENT_AGENT_DEADLINE_ENV,
    DOCUMENT_AGENT_MODEL_ENV,
    GREAT_DOCS_BUILD_COMMAND,
    assert_package_modules_unchanged,
    assert_user_guide_pages_exist,
    author_or_restore_user_guide,
    build_user_guide_agent_prompt,
    configure_great_docs_yml,
    document_agent_model,
    parse_parity_report,
    render_validation_page,
    require_cursor_api_key,
    resolve_document_agent_deadline_seconds,
    restore_user_guide_cache,
    run_cmd,
    run_user_guide_agent,
    save_user_guide_cache,
    snapshot_package_modules,
    user_guide_cache_key,
    validate_runnable_cell_rules,
    write_validation_page,
)
from src.pipeline_config import (
    DistProjectMetadata,
    PipelineConfig,
    RunnableCellRule,
    load_pipeline_config,
)


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: MagicMock,
    resume: bool = False,
) -> dict[str, Any]:
    """Install AsyncClient.launch_bridge mocks; return captured create/resume kwargs."""
    captured: dict[str, Any] = {}
    client = MagicMock()
    agents = MagicMock()

    async def fake_create(**kwargs: object) -> MagicMock:
        if resume:
            raise AssertionError("should resume, not create")
        captured["create"] = kwargs
        return agent

    async def fake_resume(agent_id: str, options: object = None) -> MagicMock:
        if not resume:
            raise AssertionError("should create, not resume")
        captured["resume_id"] = agent_id
        captured["resume_options"] = options
        return agent

    agents.create = AsyncMock(side_effect=fake_create)
    agents.resume = AsyncMock(side_effect=fake_resume)
    client.agents = agents

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=False)

    async def fake_launch_bridge(
        *_args: object, workspace: str, **_kwargs: object
    ) -> MagicMock:
        captured["workspace"] = workspace
        return client_cm

    monkeypatch.setattr(
        "src.documentation_pipeline.AsyncClient.launch_bridge",
        fake_launch_bridge,
    )
    return captured


def _finished_agent(
    *, agent_id: str = "agent-1", status: str = "finished"
) -> MagicMock:
    run = MagicMock()
    run.id = "run-1"
    run.wait = AsyncMock(return_value=MagicMock(status=status, id="run-1"))
    run.supports.return_value = False
    run.cancel = AsyncMock()

    agent = MagicMock()
    agent.agent_id = agent_id
    agent.send = AsyncMock(return_value=run)
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock(return_value=False)
    return agent


def _minimal_config(tmp_path: Path) -> PipelineConfig:
    templates = tmp_path / "templates"
    templates.mkdir()
    prompt = templates / "user-guide-agent.txt"
    prompt.write_text(
        (
            "Docs for {library_name}. Import `{api_import_path}`. "
            "Install: `{install_command}`. Package `{package_name}`. "
            "Read docs-source/guidance-note.md. "
            "Do not run great-docs, quarto render, or quarto preview.\n"
        ),
        encoding="utf-8",
    )
    guide = tmp_path / "guide.md"
    guide.write_text("# Guidance\n\nEconomist note.\n", encoding="utf-8")
    dist = tmp_path / "dist"
    package = dist / "my_model"
    package.mkdir(parents=True)
    (package / "api.py").write_text(
        "def compute_x():\n    return (1.0,)\n", encoding="utf-8"
    )
    for name in ("__init__.py", "data.py", "runtime.py", "internals.py"):
        (package / name).write_text("# generated\n", encoding="utf-8")
    return PipelineConfig(
        repo_root=tmp_path,
        workbook_path=tmp_path / "workbook.xlsx",
        guide_path=guide,
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
            repository_url="https://github.com/example/my-model",
        ),
        user_guide_agent_prompt_path=prompt,
        differential_workbook_rel=Path("data/workbook.xlsx"),
        differential_report_dir_rel=Path("data/differential/exported_library"),
        differential_graph_report_dir_rel=Path("data/differential/graph"),
        graph_output_dir=tmp_path / "artifacts" / "dependency-graph",
        graph_audit_cases=(),
    )


def test_build_user_guide_agent_prompt_contains_facts_not_outline(
    tmp_path: Path,
) -> None:
    config = _minimal_config(tmp_path)
    prompt = build_user_guide_agent_prompt(config)
    assert "My Model" in prompt
    assert "my_model.api" in prompt
    assert "docs-source/guidance-note.md" in prompt
    assert GREAT_DOCS_BUILD_COMMAND not in prompt
    assert config.dist_metadata.resolved_install_command() in prompt
    assert "Functional Overview" not in prompt
    assert "canonical_api_usage" not in prompt
    assert "make_context" not in prompt


def test_user_guide_agent_prompt_forbids_site_builds() -> None:
    config = load_pipeline_config()
    prompt = build_user_guide_agent_prompt(config)
    lowered = prompt.lower()
    assert GREAT_DOCS_BUILD_COMMAND not in prompt
    assert "do not run" in lowered
    assert "great-docs" in lowered
    assert "quarto render" in lowered
    assert "quarto preview" in lowered
    assert "iterate until it succeeds" not in lowered
    assert "that build succeeds" not in lowered
    assert "uv run --project . python" in prompt


def test_document_agent_model_defaults_to_luna(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DOCUMENT_AGENT_MODEL_ENV, raising=False)
    assert document_agent_model() == DEFAULT_DOCUMENT_AGENT_MODEL
    assert document_agent_model() == "gpt-5.6-luna"


def test_document_agent_deadline_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DOCUMENT_AGENT_DEADLINE_ENV, raising=False)
    assert (
        resolve_document_agent_deadline_seconds()
        == DEFAULT_DOCUMENT_AGENT_DEADLINE_SECONDS
    )
    monkeypatch.setenv(DOCUMENT_AGENT_DEADLINE_ENV, "90")
    assert resolve_document_agent_deadline_seconds() == 90.0


def test_require_cursor_api_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CURSOR_API_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match=CURSOR_API_KEY_ENV):
        require_cursor_api_key()


def test_run_user_guide_agent_creates_local_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _minimal_config(tmp_path)
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "cursor_test_key")
    monkeypatch.delenv(DOCUMENT_AGENT_MODEL_ENV, raising=False)

    agent = _finished_agent()
    captured = _patch_async_client(monkeypatch, agent=agent)

    run_user_guide_agent(config, cache_key="abc123")

    created = captured["create"]
    assert created["model"] == "gpt-5.6-luna"
    assert created["api_key"] == "cursor_test_key"
    local = created["local"]
    assert str(Path(local.cwd).resolve()) == str(config.dist_root.resolve())
    assert getattr(local, "setting_sources", None) is None
    assert Path(captured["workspace"]).resolve() == config.dist_root.resolve()
    agent.send.assert_awaited_once()


def test_run_user_guide_agent_fails_on_error_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _minimal_config(tmp_path)
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "cursor_test_key")

    agent = _finished_agent(status="error")
    _patch_async_client(monkeypatch, agent=agent)

    with pytest.raises(RuntimeError, match="status='error'"):
        run_user_guide_agent(config, cache_key="abc123")


def test_run_user_guide_agent_fails_on_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cursor_sdk import CursorAgentError

    config = _minimal_config(tmp_path)
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "cursor_test_key")

    agent = _finished_agent()
    agent.send = AsyncMock(
        side_effect=CursorAgentError("auth failed", is_retryable=False)
    )
    _patch_async_client(monkeypatch, agent=agent)

    with pytest.raises(RuntimeError, match="failed to start"):
        run_user_guide_agent(config, cache_key="abc123")


def test_run_user_guide_agent_resumes_active_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _minimal_config(tmp_path)
    monkeypatch.setenv(CURSOR_API_KEY_ENV, "cursor_test_key")
    monkeypatch.delenv(DOCUMENT_AGENT_MODEL_ENV, raising=False)
    from src.documentation_pipeline import write_active_agent

    write_active_agent(config, agent_id="agent-resume", cache_key="abc123")

    agent = _finished_agent(agent_id="agent-resume")
    captured = _patch_async_client(monkeypatch, agent=agent, resume=True)

    run_user_guide_agent(config, cache_key="abc123")
    assert captured["resume_id"] == "agent-resume"
    options = captured["resume_options"]
    assert options.api_key == "cursor_test_key"
    model = options.model
    model_id = model if isinstance(model, str) else getattr(model, "id", model)
    assert model_id == "gpt-5.6-luna"
    assert Path(options.local.cwd).resolve() == config.dist_root.resolve()
    assert (
        config.repo_root / ".cache" / "user-guide" / "active.json"
    ).is_file() is False


def test_cache_hit_skips_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _minimal_config(tmp_path)
    guide_root = config.dist_root / "user_guide"
    guide_root.mkdir(parents=True)
    (guide_root / "index.qmd").write_text(
        '---\ntitle: "Hi"\n---\n\nHello.\n', encoding="utf-8"
    )
    cache_key = user_guide_cache_key(config)
    save_user_guide_cache(config, cache_key=cache_key, extra_deps=["tabulate"])
    (guide_root / "index.qmd").unlink()

    async def boom_launch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("AsyncClient.launch_bridge should not run on cache hit")

    monkeypatch.setattr(
        "src.documentation_pipeline.AsyncClient.launch_bridge",
        boom_launch,
    )
    monkeypatch.setattr(
        "src.documentation_pipeline.run_user_guide_agent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no agent")),
    )

    author_or_restore_user_guide(config)
    assert (config.dist_root / "user_guide" / "index.qmd").is_file()
    pyproject = (config.dist_root / "pyproject.toml").read_text(encoding="utf-8")
    assert "tabulate" in pyproject


def test_package_module_snapshot_detects_mutation(tmp_path: Path) -> None:
    config = _minimal_config(tmp_path)
    snapshot = snapshot_package_modules(config)
    (config.package_root / "api.py").write_text(
        "def compute_x():\n    return (2.0,)\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="modified generated package module api.py"):
        assert_package_modules_unchanged(config, snapshot)


def test_empty_user_guide_fails(tmp_path: Path) -> None:
    config = _minimal_config(tmp_path)
    (config.dist_root / "user_guide").mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError, match="without writing any user_guide"):
        assert_user_guide_pages_exist(config)


def test_validate_runnable_cell_rules_rejects_forbidden_pattern(
    tmp_path: Path,
) -> None:
    config = replace(
        _minimal_config(tmp_path),
        runnable_cell_rules=(
            RunnableCellRule(pattern=r"\bmake_context\b", message="no ctx"),
        ),
    )
    guide = config.dist_root / "user_guide"
    guide.mkdir(parents=True)
    (guide / "index.qmd").write_text(
        "---\ntitle: t\n---\n\n```{python}\nmake_context()\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbidden pattern"):
        validate_runnable_cell_rules(config)


def test_write_validation_page_independent_of_agent(tmp_path: Path) -> None:
    config = _minimal_config(tmp_path)
    tests_root = config.dist_root / "tests"
    tests_root.mkdir(parents=True)
    (tests_root / "README.md").write_text("validation", encoding="utf-8")
    report = tests_root / "results" / "reference" / "parity_report.txt"
    report.parent.mkdir(parents=True)
    report.write_text(
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
    write_validation_page(config=config)
    page = (
        config.dist_root / "user_guide" / "03-excel-parity-validation.qmd"
    ).read_text(encoding="utf-8")
    assert "PASS" in page
    assert "My Model" in page


def test_configure_great_docs_yml_sets_homepage(tmp_path: Path) -> None:
    config = _minimal_config(tmp_path)
    yml = config.dist_root / "great-docs.yml"
    config.dist_root.mkdir(parents=True, exist_ok=True)
    yml.write_text("# module: yaml12\n", encoding="utf-8")
    configure_great_docs_yml(config)
    content = yml.read_text(encoding="utf-8")
    assert "module: my_model" in content
    assert "homepage: user_guide" in content
    assert 'site_url: "https://example.com/"' in content


def test_run_cmd_forces_utf8_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> MagicMock:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return MagicMock(returncode=0)

    monkeypatch.setattr("src.documentation_pipeline.subprocess.run", fake_run)
    run_cmd(["echo", "hi"])
    env = cast(dict[str, str], captured["env"])
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_parse_parity_report_requires_fields() -> None:
    with pytest.raises(ValueError, match="missing required fields"):
        parse_parity_report("Generated: x\n")


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
        library_name="Example Lib",
        package_name="example_lib",
    )
    assert "Example Lib" in page
    assert "example_lib" in page


def test_run_documentation_pipeline_monitors_document_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src import documentation_pipeline as docs

    config = _minimal_config(tmp_path)
    (config.dist_root / "great-docs.yml").write_text(
        "module: yaml12\n", encoding="utf-8"
    )
    monitored: list[str] = []

    class FakeTimer:
        def stage(self, name: str):
            from contextlib import nullcontext

            monitored.append(name)
            return nullcontext()

    monkeypatch.setattr(docs, "StageTimer", FakeTimer)
    monkeypatch.setattr(
        docs,
        "monitor_pipeline_stage",
        lambda timer, stage, **_kwargs: timer.stage(stage),
    )
    monkeypatch.setattr(docs, "configure_great_docs_yml", lambda _config: None)
    monkeypatch.setattr(docs, "author_or_restore_user_guide", lambda *_a, **_k: None)
    monkeypatch.setattr(docs, "write_validation_page", lambda **_kwargs: None)
    monkeypatch.setattr(docs, "validate_runnable_cell_rules", lambda _config: None)
    monkeypatch.setattr(docs, "run_great_docs_build", lambda _config: None)
    monkeypatch.setattr(docs, "write_docs_deploy_workflow", lambda _config: None)

    docs.run_documentation_pipeline(config)
    assert "document" in monitored


def test_load_pipeline_config_user_guide_prompt_path() -> None:
    config = load_pipeline_config()
    assert config.user_guide_agent_prompt_path.name == "user-guide-agent.txt"
    assert (
        config.repo_relative_posix_path(config.user_guide_agent_prompt_path)
        == "templates/user-guide-agent.txt"
    )


def test_restore_user_guide_cache_missing_returns_false(tmp_path: Path) -> None:
    config = _minimal_config(tmp_path)
    assert restore_user_guide_cache(config, cache_key="missing") is False
