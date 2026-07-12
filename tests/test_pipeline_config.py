from pathlib import Path

import pytest

from src.pipeline_config import (
    DistProjectMetadata,
    load_pipeline_config,
    validate_pipeline_config,
)
from src.workbook_addresses import ProjectionColumnLayout, parse_workbook_address


def test_load_pipeline_config_reads_workbook_config() -> None:
    config = load_pipeline_config()
    assert config.dist_metadata.project_name == "tiny-dsa"
    assert config.dist_metadata.package_name == "tiny_dsa"
    assert config.docstring_callback_name == "series_docs"
    assert config.workbook_path.name == "tiny-dsa.xlsx"
    assert config.guide_path.name == "tiny-dsa-guide.md"
    assert config.targets == (
        "output_baseline",
        "output_shocked",
        "output_delta",
    )
    assert config.graph_audit_cases == ()
    assert config.variation_mode == "independent"
    assert config.canonical_api_example_path.name == "canonical-api-usage.md"
    assert (
        config.repo_relative_posix_path(config.canonical_api_example_path)
        == "templates/canonical-api-usage.md"
    )


def test_load_pipeline_config_reads_variation_mode_from_workbook_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    monkeypatch.setattr(workbook_config, "VARIATION_MODE", "dominant_key_only")
    config = load_pipeline_config()
    assert config.variation_mode == "dominant_key_only"


def test_load_pipeline_config_rejects_invalid_variation_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workbook_config

    monkeypatch.setattr(workbook_config, "VARIATION_MODE", "all_keys")
    with pytest.raises(ValueError, match="VARIATION_MODE"):
        load_pipeline_config()


def test_repo_relative_posix_path_falls_back_outside_repo(tmp_path: Path) -> None:
    config = load_pipeline_config()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    assert config.repo_relative_posix_path(outside) == outside.resolve().as_posix()


def test_validate_pipeline_config_passes_for_tiny_dsa() -> None:
    config = load_pipeline_config()
    validate_pipeline_config(config)


def test_parse_workbook_address() -> None:
    assert parse_workbook_address("Inputs!C16") == ("Inputs", "C", 16)


def test_projection_column_layout_maps_outputs_to_engine() -> None:
    layout = ProjectionColumnLayout(
        engine_sheet="Engine",
        engine_columns=("C", "D"),
        outputs_sheet="Outputs",
        outputs_column_to_engine={"B": "C", "C": "D"},
        time_period_to_engine_column={1: "C", 2: "D"},
    )
    assert layout.logical_engine_column("Engine!D10") == "D"
    assert layout.logical_engine_column("Outputs!C12") == "D"
    assert layout.time_period_for_engine_column("C") == 1


def test_dist_project_metadata_install_command_without_repo() -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="Example library.",
        documentation_url="https://example.com/",
    )
    assert metadata.resolved_install_command() == "uv add forecast-kit"
