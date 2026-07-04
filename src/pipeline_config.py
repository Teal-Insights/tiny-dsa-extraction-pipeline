"""Pipeline configuration types and loading from ``workbook_config.py``."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

from src.workbook_addresses import ProjectionColumnLayout

_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DistProjectMetadata:
    project_name: str
    package_name: str
    library_name: str
    description: str
    documentation_url: str
    repository_url: str | None = None
    install_command: str | None = None
    attribution: str | None = None

    def resolved_install_command(self) -> str:
        if self.install_command is not None:
            return self.install_command
        if self.repository_url is not None:
            return f'uv add "{self.project_name} @ git+{self.repository_url}"'
        return f"uv add {self.project_name}"


@dataclass(frozen=True)
class PipelineConfig:
    repo_root: Path
    workbook_path: Path
    guide_path: Path
    bindings_path: Path
    dist_root: Path
    targets: tuple[str, ...]
    constraints: dict[str, object]
    dist_metadata: DistProjectMetadata
    docstring_callback_name: str
    projection_layout: ProjectionColumnLayout | None
    canonical_api_example_path: Path
    binding_authoring_prompt_path: Path
    section_rewrite_introduction_focus_path: Path
    section_rewrite_functional_overview_focus_path: Path
    section_rewrite_illustrative_example_focus_path: Path
    differential_workbook_rel: Path
    differential_report_dir_rel: Path
    differential_graph_report_dir_rel: Path
    graph_output_dir: Path

    @property
    def package_root(self) -> Path:
        return self.dist_root / self.dist_metadata.package_name

    @property
    def api_module_path(self) -> Path:
        return self.package_root / "api.py"

    @property
    def api_import_path(self) -> str:
        return f"{self.dist_metadata.package_name}.api"

    def repo_relative_posix_path(self, path: Path) -> str:
        """Return ``path`` relative to ``repo_root`` with forward slashes."""
        resolved = path if path.is_absolute() else self.repo_root / path
        return resolved.relative_to(self.repo_root).as_posix()


def load_pipeline_config(*, repo_root: Path | None = None) -> PipelineConfig:
    """Load workbook-specific settings from the repository ``workbook_config`` module."""
    root = repo_root or _REPO_ROOT
    user_config = importlib.import_module("workbook_config")

    workbook_path = Path(user_config.WORKBOOK_PATH)
    guide_path = Path(user_config.GUIDE_PATH)
    bindings_path = Path(user_config.BINDINGS_PATH)
    dist_root = root / "dist"
    targets = tuple(user_config.TARGETS)
    constraints = dict(user_config.CONSTRAINTS)
    dist_metadata = user_config.DIST_METADATA
    docstring_callback_name = str(user_config.DOCSTRING_CALLBACK_NAME)
    projection_layout = getattr(user_config, "PROJECTION_LAYOUT", None)

    templates_root = root / "templates"
    canonical_api_example_path = templates_root / "canonical-api-usage.md"
    binding_authoring_prompt_path = templates_root / "binding-authoring-prompt.txt"
    section_rewrite_introduction_focus_path = Path(
        getattr(
            user_config,
            "SECTION_REWRITE_INTRODUCTION_FOCUS_PATH",
            templates_root / "section-rewrite-introduction-focus.txt",
        )
    )
    section_rewrite_functional_overview_focus_path = Path(
        getattr(
            user_config,
            "SECTION_REWRITE_FUNCTIONAL_OVERVIEW_FOCUS_PATH",
            templates_root / "section-rewrite-functional-overview-focus.txt",
        )
    )
    section_rewrite_illustrative_example_focus_path = Path(
        getattr(
            user_config,
            "SECTION_REWRITE_ILLUSTRATIVE_EXAMPLE_FOCUS_PATH",
            templates_root / "section-rewrite-illustrative-example-focus.txt",
        )
    )
    differential_workbook_rel = Path(
        getattr(user_config, "DIFFERENTIAL_WORKBOOK_REL", "data/workbook.xlsx")
    )
    differential_report_dir_rel = Path(
        getattr(
            user_config,
            "DIFFERENTIAL_REPORT_DIR_REL",
            "data/differential/exported_library",
        )
    )
    differential_graph_report_dir_rel = Path(
        getattr(
            user_config,
            "DIFFERENTIAL_GRAPH_REPORT_DIR_REL",
            "data/differential/graph",
        )
    )
    graph_output_dir = root / "artifacts" / "dependency-graph"

    if not isinstance(dist_metadata, DistProjectMetadata):
        raise TypeError("workbook_config.DIST_METADATA must be a DistProjectMetadata")

    return PipelineConfig(
        repo_root=root,
        workbook_path=workbook_path,
        guide_path=guide_path,
        bindings_path=bindings_path,
        dist_root=dist_root,
        targets=targets,
        constraints=constraints,
        dist_metadata=dist_metadata,
        docstring_callback_name=docstring_callback_name,
        projection_layout=projection_layout,
        canonical_api_example_path=canonical_api_example_path,
        binding_authoring_prompt_path=binding_authoring_prompt_path,
        section_rewrite_introduction_focus_path=section_rewrite_introduction_focus_path,
        section_rewrite_functional_overview_focus_path=(
            section_rewrite_functional_overview_focus_path
        ),
        section_rewrite_illustrative_example_focus_path=(
            section_rewrite_illustrative_example_focus_path
        ),
        differential_workbook_rel=differential_workbook_rel,
        differential_report_dir_rel=differential_report_dir_rel,
        differential_graph_report_dir_rel=differential_graph_report_dir_rel,
        graph_output_dir=graph_output_dir,
    )


def validate_pipeline_config(config: PipelineConfig) -> None:
    """Fail fast when required workbook inputs are missing."""
    missing: list[str] = []
    if not config.workbook_path.is_file():
        missing.append(f"workbook: {config.workbook_path}")
    if not config.guide_path.is_file():
        missing.append(f"guide: {config.guide_path}")
    if not config.bindings_path.is_dir():
        missing.append(f"bindings directory: {config.bindings_path}")
    elif not any(config.bindings_path.glob("*.bindings.yaml")):
        missing.append(
            f"bindings YAML files under {config.bindings_path} "
            "(expected inputs.bindings.yaml and outputs.bindings.yaml)"
        )
    if not config.targets:
        missing.append("workbook_config.TARGETS (at least one extraction target)")
    if not config.constraints:
        missing.append("workbook_config.CONSTRAINTS (leaf and dynamic-ref constraints)")
    if missing:
        raise FileNotFoundError(
            "Pipeline configuration is incomplete:\n"
            + "\n".join(f"  - {item}" for item in missing)
        )


def discover_public_api_symbols(api_module_path: Path) -> tuple[str, ...]:
    """Return public function names exported from the generated ``api.py`` module."""
    import ast

    source = api_module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    return tuple(
        sorted(
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        )
    )
