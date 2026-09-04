"""Tests for Quarto user-guide helpers and dist project metadata rendering."""

from pathlib import Path

from src.pipeline_config import DistProjectMetadata
from src.qmd_python_validation import (
    DOCUMENTATION_BASELINE_DEV_DEPS,
    VALIDATION_BASELINE_DEV_DEPS,
    extract_python_cells,
    merge_dev_dependencies,
    parse_dev_dependencies_from_pyproject,
    render_dist_pyproject_toml,
    render_dist_readme_markdown,
    write_dist_pyproject,
    write_dist_readme,
)

SAMPLE_QMD = """---
title: "Example"
---

Intro.

```{python}
import polars as pl
x = 1
```

More text.

```{python}
y = x + 1
```
"""


def test_extract_python_cells_finds_all_fences() -> None:
    cells = extract_python_cells(SAMPLE_QMD)
    assert len(cells) == 2
    assert cells[0].index == 0
    assert "import polars" in cells[0].source
    assert cells[1].source.strip() == "y = x + 1"


def test_merge_dev_dependencies_deduplicates_and_preserves_order() -> None:
    merged = merge_dev_dependencies(
        ["quarto>=0.1.0", "great-docs>=0.12.0"],
        ["pandas", "quarto>=1.0.0", "tabulate"],
    )
    assert merged == [
        "quarto>=0.1.0",
        "great-docs>=0.12.0",
        "pandas",
        "tabulate",
    ]


def test_parse_dev_dependencies_from_pyproject() -> None:
    text = render_dist_pyproject_toml(
        dev_dependencies=["quarto>=0.1.0", "tabulate"],
        validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
        metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="test",
            documentation_url="https://example.com/",
        ),
    )
    deps = parse_dev_dependencies_from_pyproject(text)
    assert "quarto>=0.1.0" in deps
    assert "tabulate" in deps
    assert not any(dep.startswith("xlwings") for dep in deps)


def test_write_dist_pyproject_and_readme(tmp_path: Path) -> None:
    metadata = DistProjectMetadata(
        project_name="my-model",
        package_name="my_model",
        library_name="My Model",
        description="Example library.",
        documentation_url="https://example.com/",
        repository_url="https://github.com/example/my-model",
    )
    write_dist_pyproject(
        tmp_path,
        dev_dependencies=list(DOCUMENTATION_BASELINE_DEV_DEPS),
        validation_dependencies=list(VALIDATION_BASELINE_DEV_DEPS),
        metadata=metadata,
    )
    write_dist_readme(tmp_path, metadata=metadata)
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert 'name = "my-model"' in pyproject
    assert "great-docs>=0.12.0" in pyproject
    assert "My Model" in readme
    assert "https://example.com/" in readme
    assert metadata.resolved_install_command() in render_dist_readme_markdown(
        metadata=metadata
    )
