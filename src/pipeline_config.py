"""Pipeline configuration types and loading from ``workbook_config.py``."""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from src.graph_dependency_audit import GraphAuditCase
from src.internal_binding_coverage import InternalBindingValidationMode

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

    def repository_slug(self) -> str | None:
        """Return ``owner/repo`` when ``repository_url`` is a GitHub repository.

        The deploy workflow uses this slug as the target repository for
        publishing the generated ``dist/`` package. Returns ``None`` for
        non-GitHub or unset URLs, which disables the publish steps.
        """
        if self.repository_url is None:
            return None
        match = re.fullmatch(
            r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?",
            self.repository_url,
        )
        if match is None:
            return None
        return f"{match.group('owner')}/{match.group('repo')}"


@dataclass(frozen=True)
class RunnableCellRule:
    """Forbidden-source rule for executable ``{python}`` guide cells.

    ``pattern`` is a regex searched against each runnable cell's source at
    rewrite time; a match rejects the rewrite with ``message`` appended to the
    error. Prose and non-executable ``python`` fences are never checked, so
    fragile APIs can still be documented there.
    """

    pattern: str
    message: str


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
    user_guide_agent_prompt_path: Path
    differential_workbook_rel: Path
    differential_report_dir_rel: Path
    differential_graph_report_dir_rel: Path
    graph_output_dir: Path
    graph_audit_cases: tuple[GraphAuditCase, ...] = ()
    graph_cache_target_bundles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    blank_ranges: tuple[str, ...] = ()
    internal_binding_validation_mode: InternalBindingValidationMode = "warn"
    internal_binding_exempt_cells: frozenset[str] = frozenset()
    runnable_cell_rules: tuple[RunnableCellRule, ...] = ()

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
        try:
            return resolved.relative_to(self.repo_root).as_posix()
        except ValueError:
            return resolved.as_posix()


def _load_graph_cache_target_bundles(
    value: object,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise TypeError(
            "GRAPH_CACHE_TARGET_BUNDLES must be a tuple or list of "
            "(label, targets) pairs"
        )
    bundles: list[tuple[str, tuple[str, ...]]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError(
                "GRAPH_CACHE_TARGET_BUNDLES entries must be (label, targets) pairs; "
                f"got {entry!r} at index {index}"
            )
        label, targets = entry
        if not isinstance(label, str) or not label:
            raise ValueError(
                f"GRAPH_CACHE_TARGET_BUNDLES label at index {index} must be a non-empty string"
            )
        if not isinstance(targets, (tuple, list)) or not targets:
            raise ValueError(
                f"GRAPH_CACHE_TARGET_BUNDLES targets at index {index} must be a non-empty sequence"
            )
        bundles.append((label, tuple(str(target) for target in targets)))
    return tuple(bundles)


def _load_internal_binding_validation_mode(
    value: object,
) -> InternalBindingValidationMode:
    if value not in ("off", "warn", "error"):
        raise ValueError(
            f"INTERNAL_BINDING_VALIDATION_MODE must be off, warn, or error; got {value!r}"
        )
    return cast(InternalBindingValidationMode, value)


def _load_internal_binding_exempt_cells(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, frozenset):
        return frozenset(str(item) for item in value)
    if isinstance(value, (set, list, tuple)):
        return frozenset(str(item) for item in value)
    raise TypeError(
        "INTERNAL_BINDING_EXEMPT_CELLS must be a frozenset, set, list, or tuple "
        f"of sheet-qualified addresses; got {type(value).__name__}"
    )


def _load_runnable_cell_rules(value: object) -> tuple[RunnableCellRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise TypeError(
            "RUNNABLE_CELL_RULES must be a tuple or list of RunnableCellRule "
            "instances or (pattern, message) pairs"
        )
    rules: list[RunnableCellRule] = []
    for index, entry in enumerate(value):
        if isinstance(entry, RunnableCellRule):
            rule = entry
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            pattern, message = entry
            rule = RunnableCellRule(pattern=str(pattern), message=str(message))
        else:
            raise ValueError(
                "RUNNABLE_CELL_RULES entries must be RunnableCellRule instances "
                f"or (pattern, message) pairs; got {entry!r} at index {index}"
            )
        try:
            re.compile(rule.pattern)
        except re.error as error:
            raise ValueError(
                f"RUNNABLE_CELL_RULES pattern at index {index} is not a valid "
                f"regex: {rule.pattern!r} ({error})"
            ) from error
        rules.append(rule)
    return tuple(rules)


def _load_blank_ranges(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError(
            "BLANK_RANGES must be a sequence of sheet-qualified A1 rectangles, "
            "not a single string"
        )
    if not isinstance(value, (tuple, list)):
        raise TypeError("BLANK_RANGES must be a tuple or list of strings")
    ranges: list[str] = []
    for index, spec in enumerate(value):
        if not isinstance(spec, str) or not spec:
            raise ValueError(
                f"BLANK_RANGES[{index}] must be a non-empty string; got {spec!r}"
            )
        ranges.append(spec)
    return tuple(ranges)


def load_pipeline_config(*, repo_root: Path | None = None) -> PipelineConfig:
    """Load workbook-specific settings from the repository ``workbook_config`` module."""
    root = repo_root or _REPO_ROOT
    user_config = importlib.import_module("workbook_config")

    workbook_path = Path(user_config.WORKBOOK_PATH)
    guide_path = Path(user_config.GUIDE_PATH)
    bindings_path = Path(user_config.BINDINGS_PATH)
    dist_root = root / "dist"
    targets = tuple(user_config.TARGETS)
    constraints = dict(getattr(user_config, "CONSTRAINTS", {}))
    blank_ranges = _load_blank_ranges(getattr(user_config, "BLANK_RANGES", ()))
    dist_metadata = user_config.DIST_METADATA

    templates_root = root / "templates"
    user_guide_agent_prompt_path = Path(
        getattr(
            user_config,
            "USER_GUIDE_AGENT_PROMPT_PATH",
            templates_root / "user-guide-agent.txt",
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
    graph_audit_cases = tuple(getattr(user_config, "GRAPH_AUDIT_CASES", ()))
    graph_cache_target_bundles = _load_graph_cache_target_bundles(
        getattr(user_config, "GRAPH_CACHE_TARGET_BUNDLES", ())
    )
    internal_binding_validation_mode = _load_internal_binding_validation_mode(
        getattr(user_config, "INTERNAL_BINDING_VALIDATION_MODE", "warn")
    )
    internal_binding_exempt_cells = _load_internal_binding_exempt_cells(
        getattr(user_config, "INTERNAL_BINDING_EXEMPT_CELLS", frozenset())
    )
    runnable_cell_rules = _load_runnable_cell_rules(
        getattr(user_config, "RUNNABLE_CELL_RULES", ())
    )

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
        user_guide_agent_prompt_path=user_guide_agent_prompt_path,
        differential_workbook_rel=differential_workbook_rel,
        differential_report_dir_rel=differential_report_dir_rel,
        differential_graph_report_dir_rel=differential_graph_report_dir_rel,
        graph_output_dir=graph_output_dir,
        graph_audit_cases=graph_audit_cases,
        graph_cache_target_bundles=graph_cache_target_bundles,
        blank_ranges=blank_ranges,
        internal_binding_validation_mode=internal_binding_validation_mode,
        internal_binding_exempt_cells=internal_binding_exempt_cells,
        runnable_cell_rules=runnable_cell_rules,
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
    # Binding YAML shards may be absent or empty ``series: []`` placeholders during
    # bootstrap extract. excel-grapher 5.1.4+ loads empty placeholders; author real
    # series before export so the public API and leaf coverage are complete.
    if not config.targets:
        missing.append("workbook_config.TARGETS (at least one extraction target)")
    if missing:
        raise FileNotFoundError(
            "Pipeline configuration is incomplete:\n"
            + "\n".join(f"  - {item}" for item in missing)
        )


def discover_public_api_symbols(api_module_path: Path) -> tuple[str, ...]:
    """Return public function names exported from the generated ``api.py`` module.

    Duplicate top-level definitions (possible when codegen emits a compute once
    per scenario variant) are collapsed to a single name.
    """
    import ast

    source = api_module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    return tuple(sorted(names))
