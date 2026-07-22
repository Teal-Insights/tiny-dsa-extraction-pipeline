"""Active pipeline configuration for modules invoked during a pipeline run."""

from __future__ import annotations

from src.pipeline_config import PipelineConfig

_active_config: PipelineConfig | None = None


def activate_pipeline_config(config: PipelineConfig) -> None:
    global _active_config
    _active_config = config


def reset_pipeline_config() -> None:
    """Clear the active configuration (for tests and isolated tooling)."""
    global _active_config
    _active_config = None


def require_pipeline_config() -> PipelineConfig:
    if _active_config is None:
        raise RuntimeError(
            "Pipeline configuration is not active. Run src.extraction_pipeline first."
        )
    return _active_config


def projection_layout():
    config = require_pipeline_config()
    return config.projection_layout
