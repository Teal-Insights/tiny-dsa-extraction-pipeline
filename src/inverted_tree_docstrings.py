"""LLM docstring overlay for inverted-tree helpers (pipeline annotate stage).

Mechanical inverted-tree codegen emits placeholder docstrings. This module asks
the docstring model for Google-style prose keyed by function signature and
bindings notes, then splices the result into ``api.py`` / ``internals.py``.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.llm_json import generate_validated_json
from src.llm_providers import build_client, model_from_env
from src.pipeline_config import PipelineConfig

DOCSTRING_MODEL_ENV = "DOCSTRING_MODEL"
DOCSTRING_PROMPT_VERSION = 1
_ANNOTATE_CACHE_SCHEMA = "1.0.0"
_PACKAGE_DOCSTRING_MODULES = ("api.py", "internals.py")


class ArgDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str


class FunctionDocResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One-line summary.")
    purpose: str = Field(description="One short sentence of purpose.")
    args: tuple[ArgDoc, ...] = Field(description="One entry per function parameter.")
    returns: str = Field(description="Description of the return value.")

    @field_validator("args", mode="before")
    @classmethod
    def _tuple_args(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


@dataclass(frozen=True)
class FunctionDocRequest:
    module_name: str
    function_name: str
    parameter_names: tuple[str, ...]
    source: str
    series_notes: str
    guide_text: str


GenerateDoc = Callable[[FunctionDocRequest], FunctionDocResponse]


def docstring_cache_path(repo_root: Path) -> Path:
    return repo_root / ".cache" / "inverted-tree-docstrings.json"


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def function_doc_cache_key(request: FunctionDocRequest, *, model: str) -> str:
    payload = {
        "schema": _ANNOTATE_CACHE_SCHEMA,
        "prompt_version": DOCSTRING_PROMPT_VERSION,
        "model": model,
        "module_name": request.module_name,
        "function_name": request.function_name,
        "parameter_names": list(request.parameter_names),
        "source": request.source,
        "series_notes": request.series_notes,
        "guide_sha256": hashlib.sha256(request.guide_text.encode()).hexdigest(),
    }
    return hashlib.sha256(_stable_json(payload).encode()).hexdigest()


def load_docstring_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.is_file():
        return {}
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"invalid inverted-tree docstring cache: {cache_path}")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise TypeError(f"invalid inverted-tree docstring cache entries: {cache_path}")
    return payload


def save_docstring_cache(cache_path: Path, cache: dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def parameter_names(node: ast.FunctionDef) -> tuple[str, ...]:
    """Return signature names in definition order, excluding ``self``."""
    names: list[str] = []
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        if arg.arg == "self":
            continue
        names.append(arg.arg)
    if node.args.vararg is not None:
        names.append(node.args.vararg.arg)
    if node.args.kwarg is not None:
        names.append(node.args.kwarg.arg)
    return tuple(names)


def render_google_docstring(
    response: FunctionDocResponse, *, indent: str = "    "
) -> str:
    """Render a Google-style docstring including the surrounding quotes."""
    lines = [
        f'{indent}"""{response.summary}',
        "",
        f"{indent}{response.purpose}",
        "",
        f"{indent}Args:",
    ]
    if response.args:
        for arg in response.args:
            lines.append(f"{indent}    {arg.name}: {arg.description}")
    else:
        lines.append(f"{indent}    (none)")
    lines.extend(
        [
            "",
            f"{indent}Returns:",
            f"{indent}    {response.returns}",
            f'{indent}"""',
        ]
    )
    return "\n".join(lines) + "\n"


def replace_function_docstring(
    source: str, function_name: str, docstring_block: str
) -> str:
    """Replace the first-body docstring of ``function_name`` with ``docstring_block``."""
    tree = ast.parse(source)
    target: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            target = node
            break
    if target is None:
        raise KeyError(f"function {function_name!r} not found")
    if not target.body:
        raise ValueError(f"function {function_name!r} has an empty body")
    first = target.body[0]
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        raise TypeError(f"function {function_name!r} has no docstring to replace")
    if first.end_lineno is None:
        raise ValueError(f"function {function_name!r} docstring lacks end_lineno")
    lines = source.splitlines(keepends=True)
    start = first.lineno - 1
    end = first.end_lineno
    block = (
        docstring_block if docstring_block.endswith("\n") else docstring_block + "\n"
    )
    lines[start:end] = [block]
    return "".join(lines)


def _function_source(source: str, node: ast.FunctionDef) -> str:
    lines = source.splitlines()
    end = node.end_lineno
    if end is None:
        raise ValueError(f"function {node.name!r} lacks end_lineno")
    return "\n".join(lines[node.lineno - 1 : end])


def _iter_functions(source: str) -> tuple[ast.FunctionDef, ...]:
    tree = ast.parse(source)
    return tuple(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    )


def _notes_for(function_name: str, series_notes: Mapping[str, str]) -> str:
    if function_name in series_notes:
        return series_notes[function_name]
    if function_name.startswith("compute_"):
        series_id = function_name.removeprefix("compute_")
        return series_notes.get(series_id, "")
    return series_notes.get(function_name, "")


def _assert_args_match(
    request: FunctionDocRequest, response: FunctionDocResponse
) -> None:
    expected = request.parameter_names
    observed = tuple(arg.name for arg in response.args)
    if observed != expected:
        raise ValueError(
            f"{request.function_name}: docstring args {observed} do not match "
            f"signature {expected}"
        )


def _prompt_for_docstring(
    request: FunctionDocRequest, schema: dict[str, object]
) -> str:
    return f"""
Write the LLM-authored Google-style docstring for one inverted-tree helper.

The function already exists. Do not invent parameters. Args must be exactly
{list(request.parameter_names)} in that order. Use the user guide and series
notes for domain language. Do not mention EvalContext, ctx, setters, or
make_context.

Function: {request.module_name}.{request.function_name}

Series notes:
{request.series_notes or "(none)"}

User guide:
{request.guide_text}

Function source:
{request.source}

Response schema:
{json.dumps(schema, indent=2)}
""".strip()


def llm_generate_doc(
    request: FunctionDocRequest,
    *,
    repo_root: Path,
    cache_path: Path | None = None,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> FunctionDocResponse:
    """Ask the docstring model for one function, with a JSON cache."""
    load_dotenv(repo_root / ".env")
    model = model_from_env(DOCSTRING_MODEL_ENV)
    resolved_cache = (
        cache_path if cache_path is not None else docstring_cache_path(repo_root)
    )
    cache = {} if no_cache else load_docstring_cache(resolved_cache)
    cache_key = function_doc_cache_key(request, model=model)
    if not no_cache and not force_rebuild and cache_key in cache:
        return FunctionDocResponse.model_validate_json(cache[cache_key])
    client, provider = build_client(model)
    parsed, content = generate_validated_json(
        client=client,
        model=model,
        provider=provider,
        system_prompt=(
            "You write concise, production-quality Python docstring prose. "
            "Return only valid JSON matching the supplied schema."
        ),
        user_prompt=_prompt_for_docstring(
            request, FunctionDocResponse.model_json_schema()
        ),
        response_model=FunctionDocResponse,
    )
    _assert_args_match(request, parsed)
    if not no_cache:
        cache[cache_key] = content
        save_docstring_cache(resolved_cache, cache)
    return parsed


def apply_inverted_tree_docstrings(
    *,
    package_root: Path,
    series_notes: Mapping[str, str],
    guide_text: str,
    generate_doc: GenerateDoc | None = None,
    repo_root: Path | None = None,
) -> None:
    """Replace mechanical docstrings on public ``api`` / ``internals`` functions."""
    resolved_generate = generate_doc
    if resolved_generate is None:
        if repo_root is None:
            raise ValueError("repo_root is required when generate_doc is omitted")

        def resolved_generate(request: FunctionDocRequest) -> FunctionDocResponse:
            return llm_generate_doc(request, repo_root=repo_root)

    for module_name in _PACKAGE_DOCSTRING_MODULES:
        path = package_root / module_name
        if not path.is_file():
            raise FileNotFoundError(f"missing generated module: {path}")
        source = path.read_text(encoding="utf-8")
        function_names = tuple(node.name for node in _iter_functions(source))
        for function_name in function_names:
            node = next(
                candidate
                for candidate in _iter_functions(source)
                if candidate.name == function_name
            )
            request = FunctionDocRequest(
                module_name=module_name,
                function_name=node.name,
                parameter_names=parameter_names(node),
                source=_function_source(source, node),
                series_notes=_notes_for(node.name, series_notes),
                guide_text=guide_text,
            )
            response = resolved_generate(request)
            _assert_args_match(request, response)
            source = replace_function_docstring(
                source, node.name, render_google_docstring(response)
            )
        path.write_text(source, encoding="utf-8", newline="\n")


def series_notes_from_bindings(series_bindings: Mapping[str, object]) -> dict[str, str]:
    """Map series id → notes from a bindings document."""
    series = series_bindings.get("series")
    if not isinstance(series, Sequence):
        return {}
    notes: dict[str, str] = {}
    for entry in series:
        if not isinstance(entry, Mapping):
            continue
        series_id = entry.get("id")
        note = entry.get("notes")
        if isinstance(series_id, str) and isinstance(note, str) and note:
            notes[series_id] = note
    return notes


def annotate_exported_package(
    config: PipelineConfig,
    *,
    no_cache: bool = False,
    force_rebuild: bool = False,
) -> None:
    """LLM-annotate ``dist/<package>`` using workbook guide and binding notes."""
    from excel_grapher.series_bindings import load_series_bindings

    bindings = load_series_bindings(config.bindings_path)

    def generate_doc(request: FunctionDocRequest) -> FunctionDocResponse:
        return llm_generate_doc(
            request,
            repo_root=config.repo_root,
            no_cache=no_cache,
            force_rebuild=force_rebuild,
        )

    apply_inverted_tree_docstrings(
        package_root=config.package_root,
        series_notes=series_notes_from_bindings(bindings),
        guide_text=config.guide_path.read_text(encoding="utf-8"),
        generate_doc=generate_doc,
        repo_root=config.repo_root,
    )
