"""Helpers for Quarto user-guide cells and generated dist project metadata."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.pipeline_config import DistProjectMetadata

DOCUMENTATION_BASELINE_DEV_DEPS: tuple[str, ...] = (
    "quarto>=0.1.0",
    "great-docs>=0.12.0",
    "pandas",
    "polars",
    "matplotlib",
)

VALIDATION_DEPENDENCY_GROUP = "validation"

VALIDATION_BASELINE_DEV_DEPS: tuple[str, ...] = (
    "xlwings>=0.35.3",
    "pywin32>=311; sys_platform == 'win32'",
)


@dataclass(frozen=True)
class PythonCell:
    index: int
    source: str


_PYTHON_CELL_PATTERN = re.compile(
    r"^```\{python\}\s*\n(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)

_DEV_DEP_LINE = re.compile(r'^\s*"(?P<dep>[^"]+)",?\s*$')


def extract_python_cells(qmd_text: str) -> list[PythonCell]:
    cells: list[PythonCell] = []
    for index, match in enumerate(_PYTHON_CELL_PATTERN.finditer(qmd_text)):
        cells.append(PythonCell(index=index, source=match.group(1)))
    return cells


def merge_dev_dependencies(
    baseline: Iterable[str],
    discovered: Iterable[str],
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for dep in [*baseline, *discovered]:
        key = dep.split("[", 1)[0].split("==", 1)[0].split(">=", 1)[0].strip()
        if key in seen:
            continue
        seen.add(key)
        merged.append(dep)
    return merged


def parse_dev_dependencies_from_pyproject(pyproject_text: str) -> list[str]:
    """Extract ``[dependency-groups].dev`` package strings from a pyproject.toml."""
    lines = pyproject_text.splitlines()
    in_dependency_groups = False
    in_dev = False
    deps: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if section == "dependency-groups":
                in_dependency_groups = True
                in_dev = False
            elif section.startswith("dependency-groups."):
                in_dependency_groups = True
                in_dev = section == "dependency-groups.dev"
            else:
                in_dependency_groups = False
                in_dev = False
            continue
        if not in_dependency_groups:
            continue
        if re.match(r"^dev\s*=\s*\[", stripped):
            in_dev = True
            if "]" in stripped and not stripped.endswith("["):
                # Inline empty or single-line list: dev = []
                in_dev = False
            continue
        if in_dev:
            if stripped.startswith("]"):
                in_dev = False
                continue
            match = _DEV_DEP_LINE.match(line)
            if match is not None:
                deps.append(match.group("dep"))
    return deps


def render_dist_pyproject_toml(
    *,
    dev_dependencies: list[str],
    validation_dependencies: list[str] | None = None,
    metadata: DistProjectMetadata,
) -> str:
    dep_lines = "\n".join(f'    "{dep}",' for dep in dev_dependencies)
    validation_block = ""
    if validation_dependencies:
        validation_lines = "\n".join(f'    "{dep}",' for dep in validation_dependencies)
        validation_block = f"""
{VALIDATION_DEPENDENCY_GROUP} = [
{validation_lines}
]"""
    return f"""[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = {_toml_string(metadata.project_name)}
version = "0.1.0"
description = {_toml_string(metadata.description)}
requires-python = ">=3.13"
dependencies = [
    "fastpyxl",
    "numpy",
]

[tool.setuptools]
packages = [{_toml_string(metadata.package_name)}]

[dependency-groups]
dev = [
{dep_lines}
]{validation_block}
"""


def _toml_string(value: str) -> str:
    return json.dumps(value)


def render_dist_readme_markdown(
    *,
    metadata: DistProjectMetadata,
) -> str:
    attribution_block = f"{metadata.attribution}" if metadata.attribution else ""
    return f"""# {metadata.library_name}

{metadata.description}

## Installation

```bash
{metadata.resolved_install_command()}
```

## Documentation

See the [full documentation]({metadata.documentation_url}).

{attribution_block}"""


def write_dist_readme(
    dist_root: Path,
    *,
    metadata: DistProjectMetadata,
) -> None:
    (dist_root / "README.md").write_text(
        render_dist_readme_markdown(metadata=metadata),
        encoding="utf-8",
    )


def write_dist_pyproject(
    dist_root: Path,
    *,
    dev_dependencies: list[str],
    validation_dependencies: list[str] | None = None,
    metadata: DistProjectMetadata,
) -> None:
    pyproject_path = dist_root / "pyproject.toml"
    pyproject_path.write_text(
        render_dist_pyproject_toml(
            dev_dependencies=dev_dependencies,
            validation_dependencies=validation_dependencies,
            metadata=metadata,
        ),
        encoding="utf-8",
    )
