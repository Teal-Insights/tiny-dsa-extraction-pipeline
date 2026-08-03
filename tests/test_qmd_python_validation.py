import subprocess
from pathlib import Path
from typing import cast

import pytest
from openai import OpenAI, omit

from src.pipeline_config import DistProjectMetadata
from src.qmd_python_validation import (
    PublicApiPolicy,
    aggregate_python_cells,
    extract_python_cells,
    filter_api_signatures,
    fix_python_cell_with_llm,
    merge_dev_dependencies,
    parse_missing_package,
    render_dist_pyproject_toml,
    render_dist_readme_markdown,
    replace_python_cell,
    write_dist_readme,
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

API_POLICY = PublicApiPolicy(
    api_import_path="my_model.api",
    allowed_symbols=frozenset({"make_context", "compute_output_baseline"}),
)

SAMPLE_METADATA = DistProjectMetadata(
    project_name="my-model",
    package_name="my_model",
    library_name="My Model",
    description="Example library.",
    documentation_url="https://example.com/",
)


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content: str | None) -> None:
        self.calls: list[dict[str, object]] = []
        self._content = content

    def create(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content: str | None) -> None:
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content: str | None) -> None:
        self.chat = _FakeChat(content)


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
        metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
    )
    assert "[dependency-groups]" in text
    assert '"tabulate"' in text
    assert 'name = "my-model"' in text


def test_render_dist_pyproject_toml_uses_project_metadata() -> None:
    text = render_dist_pyproject_toml(
        dev_dependencies=[],
        metadata=DistProjectMetadata(
            project_name="forecast-kit",
            package_name="forecast_kit",
            library_name="Forecast Kit",
            description="A generated forecasting library.",
            install_command="python -m pip install forecast-kit",
            documentation_url="https://example.com/forecast-kit/",
        ),
    )

    assert 'name = "forecast-kit"' in text
    assert 'description = "A generated forecasting library."' in text
    assert 'packages = ["forecast_kit"]' in text


def test_template_metadata_description_is_single_line() -> None:
    from src.pipeline_config import load_pipeline_config

    assert "\n" not in load_pipeline_config().dist_metadata.description


def test_render_dist_pyproject_toml_description_has_no_embedded_newline() -> None:
    text = render_dist_pyproject_toml(
        dev_dependencies=[],
        metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            documentation_url="https://example.com/",
        ),
    )
    assert "\\n" not in text


def test_render_dist_pyproject_toml_excludes_multiline_attribution() -> None:
    attribution = "Created by Example Corp.\n\n![Logo](README_files/logo.png)"
    text = render_dist_pyproject_toml(
        dev_dependencies=[],
        metadata=DistProjectMetadata(
            project_name="my-model",
            package_name="my_model",
            library_name="My Model",
            description="Example library.",
            attribution=attribution,
            documentation_url="https://example.com/",
        ),
    )

    assert 'description = "Example library."' in text
    assert "Created by Example Corp." not in text
    assert "![Logo]" not in text
    assert "\\n" not in text


def test_render_dist_readme_markdown_renders_attribution_block() -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="A generated forecasting library.",
        attribution=(
            "Created by Example Org.\n\n![Example logo](https://example.com/logo.png)"
        ),
        install_command="python -m pip install forecast-kit",
        documentation_url="https://example.com/forecast-kit/",
    )

    text = render_dist_readme_markdown(metadata=metadata)

    assert "A generated forecasting library." in text
    assert "Created by Example Org." in text
    assert "![Example logo](https://example.com/logo.png)" in text


def test_render_dist_readme_markdown_omits_attribution_when_absent() -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="A generated forecasting library.",
        install_command="python -m pip install forecast-kit",
        documentation_url="https://example.com/forecast-kit/",
    )

    text = render_dist_readme_markdown(metadata=metadata)

    assert text.count("\n\n\n") == 0


def test_render_dist_readme_markdown_uses_install_command_verbatim() -> None:
    text = render_dist_readme_markdown(
        metadata=DistProjectMetadata(
            project_name="forecast-kit",
            package_name="forecast_kit",
            library_name="Forecast Kit",
            description="A generated forecasting library.",
            install_command="python -m pip install forecast-kit",
            documentation_url="https://example.com/forecast-kit/",
        )
    )

    assert text.startswith("# Forecast Kit\n")
    assert "A generated forecasting library." in text
    assert "python -m pip install forecast-kit" in text
    assert "https://example.com/forecast-kit/" in text


def test_resolved_install_command_formats_uv_git_source() -> None:
    metadata = DistProjectMetadata(
        project_name="lic-dsf",
        package_name="lic_dsf",
        library_name="LIC DSF",
        description="A generated library.",
        documentation_url="https://example.com/lic-dsf/",
        repository_url="https://github.com/Teal-Insights/py-lic-dsf",
    )
    assert (
        metadata.resolved_install_command()
        == 'uv add "lic-dsf @ git+https://github.com/Teal-Insights/py-lic-dsf"'
    )


def test_write_dist_readme_writes_root_readme(tmp_path: Path) -> None:
    metadata = DistProjectMetadata(
        project_name="forecast-kit",
        package_name="forecast_kit",
        library_name="Forecast Kit",
        description="A generated forecasting library.",
        repository_url="https://github.com/example/forecast-kit",
        documentation_url="https://example.com/forecast-kit/",
    )

    write_dist_readme(tmp_path, metadata=metadata)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == (
        render_dist_readme_markdown(metadata=metadata)
    )


def test_fix_python_cell_with_llm_uses_openai_supported_reasoning_params() -> None:
    fake = _FakeClient("print('fixed')\n")

    fixed = fix_python_cell_with_llm(
        client=cast(OpenAI, fake),
        model="gpt-5.5",
        cell_source="print(unknown)\n",
        error_message="NameError: name 'unknown' is not defined",
        qmd_label="guide.qmd",
        cell_number=1,
        api_policy=PublicApiPolicy(
            api_import_path="my_model.api",
            allowed_symbols=frozenset({"make_context"}),
        ),
    )

    assert fixed == "print('fixed')\n"
    assert len(fake.chat.completions.calls) == 1
    call = fake.chat.completions.calls[0]
    assert call["model"] == "gpt-5.5"
    assert call["reasoning_effort"] == "high"
    assert call["extra_body"] is None


def test_fix_python_cell_with_llm_includes_signatures_and_shape_guidance() -> None:
    fake = _FakeClient("set_example_series(ctx, [1.0, 2.0, 3.0])\n")
    signatures = (
        "def set_example_series(ctx, records):\n"
        "    '''Examples:\\n"
        "        set_example_series(ctx, [1.0, 2.0, 3.0])\\n"
        "    '''\n"
        "    pass\n"
        "\n"
        "def set_other(ctx, value):\n"
        "    pass\n"
    )

    fixed = fix_python_cell_with_llm(
        client=cast(OpenAI, fake),
        model="gpt-5.5",
        cell_source="set_example_series(ctx, [2.0])\n",
        error_message="ValueError: expected 3 values for positional input, got 1",
        qmd_label="01-functional-overview.qmd",
        cell_number=1,
        api_policy=PublicApiPolicy(
            api_import_path="my_model.api",
            allowed_symbols=frozenset(
                {"set_example_series", "set_other", "make_context"}
            ),
        ),
        api_signatures=signatures,
    )

    assert fixed == "set_example_series(ctx, [1.0, 2.0, 3.0])\n"
    user_prompt = cast(
        list[dict[str, str]], fake.chat.completions.calls[0]["messages"]
    )[1]["content"]
    assert "positional length mismatch" in user_prompt
    assert "keyed records" in user_prompt
    assert "set_example_series" in user_prompt
    assert "def set_example_series" in user_prompt
    assert "def set_other" not in user_prompt


def test_fix_python_cell_with_llm_routes_deepseek_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    fake = _FakeClient("print('fixed')\n")

    fixed = fix_python_cell_with_llm(
        client=cast(OpenAI, fake),
        model="deepseek-v4-pro",
        cell_source="print(unknown)\n",
        error_message="NameError: name 'unknown' is not defined",
        qmd_label="guide.qmd",
        cell_number=1,
        api_policy=PublicApiPolicy(
            api_import_path="my_model.api",
            allowed_symbols=frozenset({"make_context"}),
        ),
    )

    assert fixed == "print('fixed')\n"
    call = fake.chat.completions.calls[0]
    assert call["model"] == "deepseek-v4-pro"
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call["reasoning_effort"] is omit


def test_filter_api_signatures_returns_empty_when_no_symbols() -> None:
    signatures = (
        "def set_example_series(ctx, records):\n"
        "    pass\n"
        "\n"
        "def set_other(ctx, value):\n"
        "    pass\n"
    )
    assert filter_api_signatures(signatures, frozenset()) == ""
    assert filter_api_signatures(signatures, frozenset({"missing"})) == ""


def test_fix_python_cell_with_llm_omits_signatures_without_api_refs() -> None:
    fake = _FakeClient("print(1)\n")
    signatures = (
        "def set_example_series(ctx, records):\n"
        "    pass\n"
        "\n"
        "def set_other(ctx, value):\n"
        "    pass\n"
    )

    fix_python_cell_with_llm(
        client=cast(OpenAI, fake),
        model="gpt-5.5",
        cell_source="print(unknown)\n",
        error_message="NameError: name 'unknown' is not defined",
        qmd_label="guide.qmd",
        cell_number=1,
        api_policy=PublicApiPolicy(
            api_import_path="my_model.api",
            allowed_symbols=frozenset(
                {"set_example_series", "set_other", "make_context"}
            ),
        ),
        api_signatures=signatures,
    )

    user_prompt = cast(
        list[dict[str, str]], fake.chat.completions.calls[0]["messages"]
    )[1]["content"]
    assert "Relevant my_model.api signatures" not in user_prompt
    assert "def set_example_series" not in user_prompt
    assert "def set_other" not in user_prompt


def test_fix_python_cell_with_llm_handles_syntax_error_source() -> None:
    fake = _FakeClient("set_example_series(ctx, [1.0, 2.0, 3.0])\n")
    signatures = (
        "def set_example_series(ctx, records):\n"
        "    pass\n"
        "\n"
        "def set_other(ctx, value):\n"
        "    pass\n"
    )

    fixed = fix_python_cell_with_llm(
        client=cast(OpenAI, fake),
        model="gpt-5.5",
        cell_source="set_example_series(ctx, [2.0\n",
        error_message="SyntaxError: '(' was never closed",
        qmd_label="01-functional-overview.qmd",
        cell_number=1,
        api_policy=PublicApiPolicy(
            api_import_path="my_model.api",
            allowed_symbols=frozenset(
                {"set_example_series", "set_other", "make_context"}
            ),
        ),
        api_signatures=signatures,
    )

    assert fixed == "set_example_series(ctx, [1.0, 2.0, 3.0])\n"
    user_prompt = cast(
        list[dict[str, str]], fake.chat.completions.calls[0]["messages"]
    )[1]["content"]
    # Unparseable cells keep only allowed names still present in the source text.
    assert "def set_example_series" in user_prompt
    assert "def set_other" not in user_prompt


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


def test_validate_runnable_cell_imports_accepts_api_symbols() -> None:
    from src.qmd_python_validation import validate_runnable_cell_imports

    validate_runnable_cell_imports(
        "from my_model.api import make_context, compute_output_baseline\n",
        api_policy=PublicApiPolicy(
            api_import_path="my_model.api",
            allowed_symbols=frozenset({"make_context", "compute_output_baseline"}),
        ),
    )


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
        api_policy=API_POLICY,
        metadata=SAMPLE_METADATA,
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
        api_policy=API_POLICY,
        metadata=SAMPLE_METADATA,
        run_uv_script=fake_run_uv_script,
        client=cast(OpenAI, object()),
        model="deepseek-v4-pro",
        write_pyproject=False,
    )

    cells = validation.extract_python_cells(qmd_path.read_text(encoding="utf-8"))
    assert run_count == 2
    assert len(fix_calls) == 1
    assert fix_calls[0]["model"] == "deepseek-v4-pro"
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
            api_policy=API_POLICY,
            metadata=SAMPLE_METADATA,
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
        render_dist_pyproject_toml(
            dev_dependencies=["quarto>=0.1.0"],
            metadata=DistProjectMetadata(
                project_name="my-model",
                package_name="my_model",
                library_name="My Model",
                description="Example library.",
                documentation_url="https://example.com/",
            ),
        ),
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
        api_policy=API_POLICY,
        metadata=SAMPLE_METADATA,
        run_uv_script=fake_run_uv_script,
        write_pyproject=True,
    )

    text = pyproject_path.read_text(encoding="utf-8")
    assert '"matplotlib"' in text
