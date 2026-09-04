from dataclasses import replace
from pathlib import Path

import pytest

from src.pipeline_config import (
    DistProjectMetadata,
    RunnableCellRule,
    load_pipeline_config,
    validate_pipeline_config,
)


def test_load_pipeline_config_reads_workbook_config() -> None:
    config = load_pipeline_config()
    assert config.dist_metadata.project_name == "tiny-dsa"
    assert config.dist_metadata.package_name == "tiny_dsa"
    assert config.workbook_path.name == "tiny-dsa.xlsx"
    assert config.guide_path.name == "tiny-dsa-guide.md"
    assert config.targets == (
        "output_baseline",
        "output_shocked",
        "output_delta",
    )
    assert config.graph_audit_cases == ()
    assert config.blank_ranges == ()
    assert config.user_guide_agent_prompt_path.name == "user-guide-agent.txt"
    assert (
        config.repo_relative_posix_path(config.user_guide_agent_prompt_path)
        == "templates/user-guide-agent.txt"
    )


def test_load_pipeline_config_reads_inverted_tree_runnable_cell_rules() -> None:
    config = load_pipeline_config()
    assert len(config.runnable_cell_rules) == 2
    assert any("make_context" in rule.pattern for rule in config.runnable_cell_rules)
    assert any("set_" in rule.pattern for rule in config.runnable_cell_rules)


def test_load_pipeline_config_reads_blank_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    ranges = (
        "'Chart Data'!D46:X46",
        "PV_LC_NR1!AF30:BB30",
        "'Input 5 - Local-debt Financing'!AF108",
    )
    monkeypatch.setattr(workbook_config, "BLANK_RANGES", ranges)
    config = load_pipeline_config()
    assert config.blank_ranges == ranges


def test_load_pipeline_config_rejects_bare_string_blank_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    monkeypatch.setattr(workbook_config, "BLANK_RANGES", "Chart Data!D46:X46")
    with pytest.raises(TypeError, match="BLANK_RANGES"):
        load_pipeline_config()


def test_load_pipeline_config_defaults_blank_ranges_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    monkeypatch.delattr(workbook_config, "BLANK_RANGES")
    config = load_pipeline_config()
    assert config.blank_ranges == ()


def test_load_pipeline_config_reads_runnable_cell_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    monkeypatch.setattr(
        workbook_config,
        "RUNNABLE_CELL_RULES",
        (
            RunnableCellRule(
                pattern=r"\bcompute_fragile_\w+\s*\(",
                message="fragile computes belong in prose",
            ),
            (r"""\bset_entity\s*\([^)]*['"]Atlantis['"]""", "use a real entity"),
        ),
        raising=False,
    )
    config = load_pipeline_config()
    assert len(config.runnable_cell_rules) == 2
    assert all(
        isinstance(rule, RunnableCellRule) for rule in config.runnable_cell_rules
    )
    assert config.runnable_cell_rules[1].message == "use a real entity"


def test_load_pipeline_config_rejects_invalid_runnable_cell_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    monkeypatch.setattr(
        workbook_config,
        "RUNNABLE_CELL_RULES",
        (("[unclosed", "bad regex"),),
        raising=False,
    )
    with pytest.raises(ValueError, match="RUNNABLE_CELL_RULES"):
        load_pipeline_config()

    monkeypatch.setattr(
        workbook_config,
        "RUNNABLE_CELL_RULES",
        ("not-a-pair",),
        raising=False,
    )
    with pytest.raises(ValueError, match="RUNNABLE_CELL_RULES"):
        load_pipeline_config()


def test_repo_relative_posix_path_falls_back_outside_repo(tmp_path: Path) -> None:
    config = load_pipeline_config()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    assert config.repo_relative_posix_path(outside) == outside.resolve().as_posix()


def test_validate_pipeline_config_passes_for_tiny_dsa() -> None:
    config = load_pipeline_config()
    validate_pipeline_config(config)


def test_validate_pipeline_config_allows_empty_bindings_directory(
    synthetic_pipeline_config_fixture,
    tmp_path: Path,
) -> None:
    """Bootstrap extract may start before any ``*.bindings.yaml`` shards exist."""
    from dataclasses import replace

    bindings = tmp_path / "bindings"
    bindings.mkdir()
    config = replace(synthetic_pipeline_config_fixture, bindings_path=bindings)
    validate_pipeline_config(config)


def test_dist_project_metadata_install_command_without_repo() -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="Example library.",
        documentation_url="https://example.com/",
    )
    assert metadata.resolved_install_command() == "uv add forecast-kit"


def test_dist_project_metadata_repository_slug_from_github_url() -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="Example library.",
        documentation_url="https://example.com/",
        repository_url="https://github.com/example-org/forecast-kit",
    )
    assert metadata.repository_slug() == "example-org/forecast-kit"

    trailing = replace(
        metadata, repository_url="https://github.com/example-org/forecast-kit.git"
    )
    assert trailing.repository_slug() == "example-org/forecast-kit"


def test_dist_project_metadata_repository_slug_without_repo() -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="Example library.",
        documentation_url="https://example.com/",
    )
    assert metadata.repository_slug() is None

    non_github = replace(
        metadata, repository_url="https://gitlab.com/example-org/forecast-kit"
    )
    assert non_github.repository_slug() is None
