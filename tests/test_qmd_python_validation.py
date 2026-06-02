from pathlib import Path

import pytest

from src.qmd_python_validation import (
    aggregate_python_cells,
    extract_python_cells,
    merge_dev_dependencies,
    parse_missing_package,
    replace_python_cell,
    render_dist_pyproject_toml,
)


SAMPLE_QMD = """---
title: "Example"
---

Intro text.

```{python}
import pandas as pd
x = 1
```

More text.

```{python}
print(x + pd.Series([1]))
```
"""


def test_extract_python_cells_finds_all_fences() -> None:
    cells = extract_python_cells(SAMPLE_QMD)
    assert len(cells) == 2
    assert "import pandas as pd" in cells[0].source
    assert "print(x + pd.Series" in cells[1].source


def test_aggregate_python_cells_adds_source_markers() -> None:
    cells = extract_python_cells(SAMPLE_QMD)
    script = aggregate_python_cells(cells, qmd_label="example.qmd")
    assert "# qmd: example.qmd cell 1" in script
    assert "# qmd: example.qmd cell 2" in script
    assert "import pandas as pd" in script


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ModuleNotFoundError: No module named 'tabulate'", "tabulate"),
        (
            "ImportError: `Import tabulate` failed.  Use pip or conda to install the tabulate package.",
            "tabulate",
        ),
        ("No module named 'numpy'", "numpy"),
    ],
)
def test_parse_missing_package(stderr: str, expected: str) -> None:
    assert parse_missing_package(stderr) == expected


def test_parse_missing_package_returns_none_for_unrelated_errors() -> None:
    assert parse_missing_package("NameError: name 'ctx' is not defined") is None


def test_replace_python_cell_updates_only_target_cell() -> None:
    updated = replace_python_cell(SAMPLE_QMD, cell_index=0, new_source="y = 2\n")
    cells = extract_python_cells(updated)
    assert cells[0].source.strip() == "y = 2"
    assert "print(x + pd.Series" in cells[1].source


def test_merge_dev_dependencies_deduplicates_and_preserves_order() -> None:
    merged = merge_dev_dependencies(
        ["quarto>=0.1.0", "great-docs>=0.12.0"],
        ["pandas", "tabulate", "pandas"],
    )
    assert merged == [
        "quarto>=0.1.0",
        "great-docs>=0.12.0",
        "pandas",
        "tabulate",
    ]


def test_render_dist_pyproject_toml_includes_dev_dependencies() -> None:
    text = render_dist_pyproject_toml(
        dev_dependencies=["quarto>=0.1.0", "great-docs>=0.12.0", "tabulate"],
    )
    assert "[dependency-groups]" in text
    assert '"tabulate"' in text
    assert 'name = "tiny-dsa"' in text


def test_validate_qmd_files_records_discovered_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import qmd_python_validation as validation

    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    qmd_path = dist_root / "user_guide" / "page.qmd"
    qmd_path.parent.mkdir(parents=True)
    qmd_path.write_text(
        """---
title: "T"
---

```{python}
import tabulate
tabulate.tabulate([[1]])
```
""",
        encoding="utf-8",
    )
    script_path = dist_root / "_validate_user_guide_cells.py"
    calls: list[list[str]] = []

    def fake_run_uv_script(
        *,
        dist_root: Path,
        script_path: Path,
        with_packages: list[str],
    ) -> validation.ScriptRunResult:
        calls.append(list(with_packages))
        if "tabulate" not in with_packages:
            return validation.ScriptRunResult(
                returncode=1,
                stdout="",
                stderr="ModuleNotFoundError: No module named 'tabulate'",
            )
        return validation.ScriptRunResult(returncode=0, stdout="", stderr="")

    discovered = validation.validate_qmd_files(
        dist_root=dist_root,
        qmd_paths=[qmd_path],
        run_uv_script=fake_run_uv_script,
    )

    assert discovered == ["tabulate"]
    assert "tabulate" in calls[-1]
    assert not script_path.is_file()


def test_validate_qmd_files_removes_script_on_failure(tmp_path: Path) -> None:
    from src import qmd_python_validation as validation

    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    qmd_path = dist_root / "user_guide" / "page.qmd"
    qmd_path.parent.mkdir(parents=True)
    qmd_path.write_text(
        """```{python}
raise RuntimeError("boom")
```""",
        encoding="utf-8",
    )
    script_path = dist_root / validation.VALIDATION_SCRIPT_NAME

    def fake_run_uv_script(**kwargs: object) -> validation.ScriptRunResult:
        return validation.ScriptRunResult(
            returncode=1,
            stdout="",
            stderr="RuntimeError: boom",
        )

    with pytest.raises(RuntimeError, match="Runnable QMD validation failed"):
        validation.validate_qmd_files(
            dist_root=dist_root,
            qmd_paths=[qmd_path],
            run_uv_script=fake_run_uv_script,
            write_pyproject=False,
        )

    assert not script_path.is_file()


def test_validate_qmd_files_writes_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import qmd_python_validation as validation

    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    pyproject_path = dist_root / "pyproject.toml"
    pyproject_path.write_text(
        render_dist_pyproject_toml(dev_dependencies=["quarto>=0.1.0"]),
        encoding="utf-8",
    )
    qmd_path = dist_root / "user_guide" / "page.qmd"
    qmd_path.parent.mkdir(parents=True)
    qmd_path.write_text(
        """```{python}
import matplotlib.pyplot as plt
plt.plot([1, 2])
```""",
        encoding="utf-8",
    )

    def fake_run_uv_script(**kwargs: object) -> validation.ScriptRunResult:
        return validation.ScriptRunResult(returncode=0, stdout="", stderr="")

    validation.validate_qmd_files(
        dist_root=dist_root,
        qmd_paths=[qmd_path],
        run_uv_script=fake_run_uv_script,
        write_pyproject=True,
    )

    text = pyproject_path.read_text(encoding="utf-8")
    assert '"matplotlib"' in text
