import subprocess
from pathlib import Path
from typing import cast

import pytest
from openai import OpenAI

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


def test_default_run_uv_script_forces_utf8_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import qmd_python_validation as validation

    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    script_path = dist_root / "_validate_user_guide_cells.py"
    script_path.write_text("print('box drawing: ┌')\n", encoding="utf-8")
    run_kwargs: dict[str, object] = {}

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        run_kwargs.update(kwargs)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    result = validation.default_run_uv_script(
        dist_root=dist_root,
        script_path=script_path,
        with_packages=[],
    )

    env = cast(dict[str, str], run_kwargs["env"])
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert run_kwargs["encoding"] == "utf-8"
    assert run_kwargs["errors"] == "replace"
    assert result.stdout == "ok"


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


def test_validate_qmd_files_rewrites_runtime_error_cell(
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
x = 1
```

```{python}
table_1 = scenarios.round(2)
print(table_1)
```
""",
        encoding="utf-8",
    )
    run_count = 0
    fix_calls: list[dict[str, object]] = []

    def fake_run_uv_script(
        *,
        dist_root: Path,
        script_path: Path,
        with_packages: list[str],
    ) -> validation.ScriptRunResult:
        nonlocal run_count
        run_count += 1
        script_text = script_path.read_text(encoding="utf-8")
        if ".round(2)" not in script_text:
            return validation.ScriptRunResult(returncode=0, stdout="", stderr="")
        line_number = script_text.splitlines().index("table_1 = scenarios.round(2)") + 1
        return validation.ScriptRunResult(
            returncode=1,
            stdout="",
            stderr=(
                f'  File "{script_path}", line {line_number}, in <module>\n'
                "AttributeError: 'DataFrame' object has no attribute 'round'"
            ),
        )

    def fake_fix_python_cell_with_llm(**kwargs: object) -> str:
        fix_calls.append(kwargs)
        return "table_1 = scenarios\nprint(table_1)\n"

    monkeypatch.setattr(
        validation,
        "fix_python_cell_with_llm",
        fake_fix_python_cell_with_llm,
    )

    validation.validate_qmd_files(
        dist_root=dist_root,
        qmd_paths=[qmd_path],
        run_uv_script=fake_run_uv_script,
        client=cast(OpenAI, object()),
        write_pyproject=False,
    )

    cells = validation.extract_python_cells(qmd_path.read_text(encoding="utf-8"))
    assert run_count == 2
    assert len(fix_calls) == 1
    assert fix_calls[0]["cell_number"] == 2
    assert fix_calls[0]["error_message"] == (
        f'File "{dist_root / validation.VALIDATION_SCRIPT_NAME}", line 5, in <module>\n'
        "AttributeError: 'DataFrame' object has no attribute 'round'"
    )
    assert cells[0].source.strip() == "x = 1"
    assert cells[1].source.strip() == "table_1 = scenarios\nprint(table_1)"


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
