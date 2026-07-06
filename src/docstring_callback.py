"""LLM-authored series docstring callback registration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from excel_grapher.exporter import (
    FieldDoc as SeriesFieldDoc,
    SeriesFunctionDoc,
    register_series_docstring_callback,
)
from pydantic import BaseModel, ConfigDict, Field, create_model

from src.llm_json import generate_validated_json
from src.llm_providers import build_client, model_from_env
from src.pipeline_config import PipelineConfig

DOCSTRING_MODEL_ENV = "DOCSTRING_MODEL"
DOCSTRING_PROMPT_VERSION = 3


def pascal_case(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_"))


def build_doc_response_model(ctx) -> type[BaseModel]:
    model_name = pascal_case(ctx.contract.series_id)
    field_description_model = create_model(
        f"{model_name}FieldDescription",
        __config__=ConfigDict(extra="forbid"),
        description=(
            str,
            Field(
                description=(
                    "Concise user-facing description. "
                    "Do not restate deterministic expected values."
                )
            ),
        ),
    )
    field_definitions: dict[str, Any] = {
        field_name: (
            field_description_model,
            Field(description=f"Description for `{field_name}`."),
        )
        for field_name in ctx.contract.fields
    }
    field_descriptions_model = create_model(
        f"{model_name}FieldDescriptions",
        __config__=ConfigDict(extra="forbid"),
        **field_definitions,
    )

    return create_model(
        f"{model_name}DocResponse",
        __config__=ConfigDict(extra="forbid"),
        summary=(
            str,
            Field(
                description="One-line summary for the generated series API function."
            ),
        ),
        purpose=(
            str,
            Field(
                description=(
                    "One short sentence explaining what this function updates or returns."
                )
            ),
        ),
        record_matching=(
            str,
            Field(
                description=(
                    "One short sentence explaining how records relate to workbook cells."
                )
            ),
        ),
        field_descriptions=(
            field_descriptions_model,
            Field(description="Descriptions for the exact record fields."),
        ),
    )


def prompt_for_docstring(guide_text: str, ctx, response_schema: dict) -> str:
    contract_json = json.dumps(asdict(ctx.contract), indent=2, default=str)
    series_json = json.dumps(ctx.series, indent=2, default=str)
    schema_json = json.dumps(response_schema, indent=2)
    return f"""
Write the LLM-authored parts of a Python docstring for one generated
series API function.

Use the user guide for domain language. Use the deterministic contract and
series binding as hard constraints. The code generator will render required
fields, optional fields, expected constants, source binding details, and
examples from the deterministic contract.

Only return JSON data that matches the response schema. Do not include
Markdown. Do not invent fields, ranges, units, accepted keys, or examples.
For fields with an expected_value in the contract, describe the field's role
only; the template will add the expected value.

For input setter functions, avoid language that says the setter validates
input domains, units, or context constants. Depending on layout, the setter
accepts a scalar, a single record, a list of records, a 1D sequence of
measure values, or a tidy DataFrame; it normalizes these into records and
checks record shape and key matching, but does not validate domains or units.

Function name: {ctx.function_name}
Function kind: {ctx.function_kind}

User guide:
{guide_text}

Series binding:
{series_json}

Deterministic docstring contract:
{contract_json}

Response schema:
{schema_json}
""".strip()


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def docstring_cache_path(repo_root: Path) -> Path:
    return repo_root / ".cache" / "series-docstrings.json"


def docstring_cache_key(ctx, response_schema: dict, guide_text: str, model: str) -> str:
    payload = {
        "model": model,
        "prompt_version": DOCSTRING_PROMPT_VERSION,
        "function_name": ctx.function_name,
        "function_kind": str(ctx.function_kind),
        "series": ctx.series,
        "contract": asdict(ctx.contract),
        "response_schema": response_schema,
        "guide_sha256": hashlib.sha256(guide_text.encode()).hexdigest(),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def load_docstring_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    return json.loads(cache_path.read_text(encoding="utf-8"))


def save_docstring_cache(cache_path: Path, cache: dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def configure_docstring_callback(config: PipelineConfig) -> str:
    """Register the OpenAI docstring callback for this extraction project."""
    load_dotenv(config.repo_root / ".env")
    guide_text = config.guide_path.read_text(encoding="utf-8")
    cache_path = docstring_cache_path(config.repo_root)
    callback_name = config.docstring_callback_name

    def openai_series_docstring(ctx) -> SeriesFunctionDoc:
        ResponseModel = build_doc_response_model(ctx)
        schema = ResponseModel.model_json_schema()
        model = model_from_env(DOCSTRING_MODEL_ENV)
        cache = load_docstring_cache(cache_path)
        cache_key = docstring_cache_key(ctx, schema, guide_text, model)
        if cache_key in cache:
            content = cache[cache_key]
        else:
            client, provider = build_client(model)
            _, content = generate_validated_json(
                client=client,
                model=model,
                provider=provider,
                system_prompt=(
                    "You write concise, production-quality Python docstring "
                    "prose. Return only valid JSON matching the supplied schema."
                ),
                user_prompt=prompt_for_docstring(guide_text, ctx, schema),
                response_model=ResponseModel,
            )
            cache[cache_key] = content
            save_docstring_cache(cache_path, cache)

        doc_data = ResponseModel.model_validate_json(content)
        parsed = doc_data.model_dump()
        field_descriptions = parsed["field_descriptions"]
        return SeriesFunctionDoc(
            summary=parsed["summary"],
            purpose=parsed["purpose"],
            record_matching=parsed["record_matching"],
            field_descriptions={
                field_name: SeriesFieldDoc(
                    description=field_descriptions[field_name]["description"]
                )
                for field_name in ctx.contract.fields
            },
        )

    register_series_docstring_callback(
        callback_name,
        openai_series_docstring,
        replace=True,
    )
    return callback_name
