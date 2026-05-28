import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from excel_grapher.exporter import (
    FieldDoc as SeriesFieldDoc,
    SeriesFunctionDoc,
    register_series_docstring_callback,
)
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, create_model

repo_root = Path(__file__).resolve().parents[1]
guide_path = repo_root / "data/tiny-dsa-guide.md"

DOCSTRING_MODEL = "deepseek-v4-pro"
DOCSTRING_PROMPT_VERSION = 2
DOCSTRING_CACHE_PATH = repo_root / ".cache/series-docstrings.json"
CALLBACK_NAME = "tiny_dsa_series_docs"


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
input domains, units, or context constants. The setter currently checks
record shape and key matching.

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


load_dotenv(repo_root / ".env")
guide_text = guide_path.read_text(encoding="utf-8")


def stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def docstring_cache_key(ctx, response_schema: dict, guide_text: str) -> str:
    payload = {
        "model": DOCSTRING_MODEL,
        "prompt_version": DOCSTRING_PROMPT_VERSION,
        "function_name": ctx.function_name,
        "function_kind": str(ctx.function_kind),
        "series": ctx.series,
        "contract": asdict(ctx.contract),
        "response_schema": response_schema,
        "guide_sha256": hashlib.sha256(guide_text.encode()).hexdigest(),
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def load_docstring_cache() -> dict[str, str]:
    if not DOCSTRING_CACHE_PATH.exists():
        return {}
    return json.loads(DOCSTRING_CACHE_PATH.read_text(encoding="utf-8"))


def save_docstring_cache(cache: dict[str, str]) -> None:
    DOCSTRING_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCSTRING_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def deepseek_series_docstring(ctx) -> SeriesFunctionDoc:
    ResponseModel = build_doc_response_model(ctx)
    schema = ResponseModel.model_json_schema()
    cache = load_docstring_cache()
    cache_key = docstring_cache_key(ctx, schema, guide_text)
    if cache_key in cache:
        content = cache[cache_key]
    else:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is required to generate uncached docstrings"
            )
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        response = client.chat.completions.create(
            model=DOCSTRING_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write concise, production-quality Python docstring "
                        "prose. Return only valid JSON matching the supplied schema."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt_for_docstring(guide_text, ctx, schema),
                },
            ],
            stream=False,
            reasoning_effort="high",
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "enabled"}},
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("DeepSeek returned empty docstring content")
        ResponseModel.model_validate_json(content)
        cache[cache_key] = content
        save_docstring_cache(cache)

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


def available_docstring_callback() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY") or DOCSTRING_CACHE_PATH.exists():
        return CALLBACK_NAME
    return None


register_series_docstring_callback(
    CALLBACK_NAME,
    deepseek_series_docstring,
    replace=True,
)
