"""Validate and fix runnable Python cells in generated Quarto user guides."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

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

DOCUMENTATION_BASELINE_RUNTIME_WITH: tuple[str, ...] = (
    "pandas",
    "polars",
    "matplotlib",
)

MAX_IMPORT_FIX_ATTEMPTS = 20
MAX_LLM_CELL_FIX_ATTEMPTS_PER_CELL = 2
MAX_LLM_CELL_FIX_ATTEMPTS_TOTAL = 5

VALIDATION_SCRIPT_NAME = "_validate_user_guide_cells.py"


@dataclass(frozen=True)
class PublicApiPolicy:
    api_import_path: str
    allowed_symbols: frozenset[str]


_PYTHON_CELL_PATTERN = re.compile(
    r"^```\{python\}\s*\n(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)

_MODULE_NOT_FOUND_PATTERN = re.compile(
    r"No module named ['\"](?P<name>[^'\"]+)['\"]",
)
_OPTIONAL_IMPORT_PATTERN = re.compile(r"`Import (?P<name>[^`]+)` failed")

_NAME_ERROR_PATTERN = re.compile(
    r"NameError: (?P<message>.+)",
    re.DOTALL,
)

_CELL_FIX_MODEL = "gpt-5.5"


@dataclass(frozen=True)
class PythonCell:
    index: int
    source: str


@dataclass(frozen=True)
class ScriptRunResult:
    returncode: int
    stdout: str
    stderr: str


def extract_python_cells(qmd_text: str) -> list[PythonCell]:
    cells: list[PythonCell] = []
    for index, match in enumerate(_PYTHON_CELL_PATTERN.finditer(qmd_text)):
        cells.append(PythonCell(index=index, source=match.group(1)))
    return cells


def replace_python_cell(qmd_text: str, cell_index: int, new_source: str) -> str:
    matches = list(_PYTHON_CELL_PATTERN.finditer(qmd_text))
    if cell_index < 0 or cell_index >= len(matches):
        raise IndexError(f"python cell index out of range: {cell_index}")
    match = matches[cell_index]
    if not new_source.endswith("\n"):
        new_source = f"{new_source}\n"
    return qmd_text[: match.start(1)] + new_source + qmd_text[match.end(1) :]


def aggregate_python_cells(cells: Iterable[PythonCell], *, qmd_label: str) -> str:
    parts: list[str] = []
    for cell in cells:
        parts.append(f"# qmd: {qmd_label} cell {cell.index + 1}")
        parts.append(cell.source.rstrip("\n"))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def parse_missing_package(error_text: str) -> str | None:
    optional_match = _OPTIONAL_IMPORT_PATTERN.search(error_text)
    if optional_match is not None:
        return optional_match.group("name")
    module_match = _MODULE_NOT_FOUND_PATTERN.search(error_text)
    if module_match is not None:
        return module_match.group("name")
    return None


def parse_name_error(error_text: str) -> str | None:
    match = _NAME_ERROR_PATTERN.search(error_text)
    if match is None:
        return None
    return match.group("message").strip()


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


def build_validation_script(qmd_paths: Iterable[Path]) -> str:
    sections: list[str] = []
    for qmd_path in qmd_paths:
        qmd_text = qmd_path.read_text(encoding="utf-8")
        cells = extract_python_cells(qmd_text)
        if not cells:
            continue
        sections.append(
            aggregate_python_cells(cells, qmd_label=qmd_path.name),
        )
    return "\n\n".join(sections)


def default_run_uv_script(
    *,
    dist_root: Path,
    script_path: Path,
    with_packages: list[str],
) -> ScriptRunResult:
    script_relative = script_path.relative_to(dist_root)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    command = [
        "uv",
        "run",
        "--project",
        str(dist_root.resolve()),
        *[arg for package in with_packages for arg in ("--with", package)],
        "python",
        str(script_relative),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        text=True,
        cwd=str(dist_root.resolve()),
    )
    return ScriptRunResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def validate_runnable_cell_imports(
    source: str,
    *,
    api_policy: PublicApiPolicy,
) -> None:
    module = ast.parse(source)
    for node in ast.walk(module):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != api_policy.api_import_path:
            continue
        for alias in node.names:
            symbol = alias.name
            if symbol not in api_policy.allowed_symbols:
                raise ValueError(
                    f"invalid {api_policy.api_import_path} import {symbol!r}; "
                    f"allowed: {sorted(api_policy.allowed_symbols)}"
                )


def fix_python_cell_with_llm(
    *,
    client: OpenAI,
    cell_source: str,
    error_message: str,
    qmd_label: str,
    cell_number: int,
    api_policy: PublicApiPolicy,
) -> str:
    allowed = ", ".join(sorted(api_policy.allowed_symbols))
    response = client.chat.completions.create(
        model=_CELL_FIX_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You fix runnable Quarto Python cells for library documentation. "
                    "Return only the corrected Python source code with no fences or commentary. "
                    f"Never import Workbook or other symbols that are not exported by {api_policy.api_import_path}."
                ),
            },
            {
                "role": "user",
                "content": f"""
Fix this runnable Python cell from {qmd_label} (cell {cell_number}).

Constraints:
- Use only the Python standard library, polars, and matplotlib.
- Tabulate compute_* results with polars when returning record lists.
- Keep {api_policy.api_import_path} usage intact.
- {api_policy.api_import_path} exports only: {allowed}.
- Do not import Workbook or any other symbol from {api_policy.api_import_path}.
- Return only valid Python source for the cell body.

Execution error:
{error_message}

Cell source:
{cell_source}
""".strip(),
            },
        ],
        stream=False,
        reasoning_effort="high",
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("LLM returned empty Python cell fix")
    fixed = content.strip()
    if fixed.startswith("```"):
        fixed = re.sub(r"^```(?:python)?\s*\n?", "", fixed)
        fixed = re.sub(r"\n?```\s*$", "", fixed)
    return fixed.strip() + "\n"


def _cell_number_from_script(script_text: str, error_text: str) -> int | None:
    cell_numbers = [
        int(value) for value in re.findall(r"# qmd: .+ cell (\d+)", script_text)
    ]
    if not cell_numbers:
        return None
    line_match = re.search(r'File ".*", line (\d+)', error_text)
    if line_match is None:
        return cell_numbers[-1]
    line_number = int(line_match.group(1))
    current_line = 0
    current_cell = cell_numbers[0]
    for line in script_text.splitlines():
        marker = re.match(r"# qmd: .+ cell (\d+)", line)
        if marker is not None:
            current_cell = int(marker.group(1))
        current_line += 1
        if current_line >= line_number:
            return current_cell
    return current_cell


def _qmd_label_from_script(
    *,
    script_text: str,
    cell_number: int,
) -> str | None:
    label_match = re.search(
        rf"# qmd: (.+) cell {cell_number}\n",
        script_text,
    )
    if label_match is None:
        return None
    return label_match.group(1)


def _apply_runtime_cell_fix(
    *,
    script_text: str,
    error_text: str,
    ordered_paths: list[Path],
    client: OpenAI,
    api_policy: PublicApiPolicy,
) -> None:
    cell_number = _cell_number_from_script(script_text, error_text)
    if cell_number is None:
        raise RuntimeError(f"Could not map runtime error to a QMD cell:\n{error_text}")
    qmd_label = _qmd_label_from_script(
        script_text=script_text,
        cell_number=cell_number,
    )
    if qmd_label is None:
        raise RuntimeError(f"Could not map runtime error to a QMD file:\n{error_text}")
    qmd_path = next(
        (path for path in ordered_paths if path.name == qmd_label),
        None,
    )
    if qmd_path is None:
        raise RuntimeError(f"Could not find QMD file for label {qmd_label!r}")
    qmd_text = qmd_path.read_text(encoding="utf-8")
    cell = extract_python_cells(qmd_text)[cell_number - 1]
    fixed_source = fix_python_cell_with_llm(
        client=client,
        cell_source=cell.source,
        error_message=error_text.strip(),
        qmd_label=qmd_label,
        cell_number=cell_number,
        api_policy=api_policy,
    )
    validate_runnable_cell_imports(fixed_source, api_policy=api_policy)
    qmd_path.write_text(
        replace_python_cell(qmd_text, cell.index, fixed_source),
        encoding="utf-8",
    )


def _remove_validation_script(script_path: Path) -> None:
    if script_path.is_file():
        script_path.unlink()


def validate_qmd_files(
    *,
    dist_root: Path,
    qmd_paths: Iterable[Path],
    api_policy: PublicApiPolicy,
    metadata: DistProjectMetadata,
    run_uv_script: Callable[..., ScriptRunResult] | None = None,
    client: OpenAI | None = None,
    write_pyproject: bool = True,
) -> list[str]:
    """Execute aggregated runnable cells and record extra dev dependencies."""
    runner = default_run_uv_script if run_uv_script is None else run_uv_script
    ordered_paths = [path for path in qmd_paths if path.is_file()]
    script_path = dist_root / VALIDATION_SCRIPT_NAME
    discovered_packages: list[str] = []
    with_packages = list(DOCUMENTATION_BASELINE_RUNTIME_WITH)
    llm_fix_attempts_total = 0
    llm_fix_attempts_by_cell: dict[tuple[str, int], int] = {}

    try:
        for _attempt in range(MAX_IMPORT_FIX_ATTEMPTS):
            script_text = build_validation_script(ordered_paths)
            if not script_text.strip():
                break
            script_path.write_text(script_text, encoding="utf-8")
            result = runner(
                dist_root=dist_root,
                script_path=script_path,
                with_packages=with_packages,
            )
            if result.returncode == 0:
                break

            error_text = f"{result.stdout}\n{result.stderr}"
            missing_package = parse_missing_package(error_text)
            if missing_package is not None:
                if missing_package not in with_packages:
                    with_packages.append(missing_package)
                    discovered_packages.append(missing_package)
                continue

            cell_number = _cell_number_from_script(script_text, error_text)
            qmd_label = (
                _qmd_label_from_script(script_text=script_text, cell_number=cell_number)
                if cell_number is not None
                else None
            )
            if cell_number is not None and qmd_label is not None and client is not None:
                fix_key = (qmd_label, cell_number)
                fix_attempts_for_cell = llm_fix_attempts_by_cell.get(fix_key, 0)
                if (
                    llm_fix_attempts_total >= MAX_LLM_CELL_FIX_ATTEMPTS_TOTAL
                    or fix_attempts_for_cell >= MAX_LLM_CELL_FIX_ATTEMPTS_PER_CELL
                ):
                    raise RuntimeError(
                        "Exceeded LLM cell fix attempts while validating "
                        f"runnable QMD cells:\n{error_text.strip()}"
                    )
                _apply_runtime_cell_fix(
                    script_text=script_text,
                    error_text=error_text,
                    ordered_paths=ordered_paths,
                    client=client,
                    api_policy=api_policy,
                )
                llm_fix_attempts_total += 1
                llm_fix_attempts_by_cell[fix_key] = fix_attempts_for_cell + 1
                continue

            raise RuntimeError(
                "Runnable QMD validation failed:\n"
                f"{error_text.strip() or '(no output)'}"
            )
        else:
            raise RuntimeError(
                f"Exceeded {MAX_IMPORT_FIX_ATTEMPTS} import-fix attempts while "
                "validating runnable QMD cells"
            )

        if write_pyproject:
            write_dist_pyproject(
                dist_root,
                dev_dependencies=merge_dev_dependencies(
                    DOCUMENTATION_BASELINE_DEV_DEPS,
                    discovered_packages,
                ),
                validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
                metadata=metadata,
            )

        return discovered_packages
    finally:
        _remove_validation_script(script_path)
