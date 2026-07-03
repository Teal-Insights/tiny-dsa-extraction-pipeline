"""Live smoke tests for per-provider model switching.

These make real, billed API calls, so they are skipped unless
``LIVE_LLM_TESTS`` is set in the environment. Each provider is exercised end to
end through :func:`generate_validated_json`, which selects the structured or
JSON-object call shape from the provider config.

Run with, for example:

    LIVE_LLM_TESTS=1 uv run pytest tests/test_llm_providers_live.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from src.llm_json import generate_validated_json
from src.llm_providers import build_client, provider_for_model

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

LIVE = os.environ.get("LIVE_LLM_TESTS")

MODELS = ["gpt-5.5", "glm-5.2", "deepseek-v4-pro"]


class Capital(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(description="The country name, echoed back exactly.")
    capital: str = Field(description="The capital city of that country.")


@pytest.mark.skipif(not LIVE, reason="set LIVE_LLM_TESTS=1 to run billed API calls")
@pytest.mark.parametrize("model", MODELS)
def test_provider_returns_valid_structured_json(model: str) -> None:
    config = provider_for_model(model)
    if not os.environ.get(config.api_key_env):
        pytest.skip(f"{config.api_key_env} not set")

    client, provider = build_client(model)
    schema = json.dumps(Capital.model_json_schema(), indent=2)
    parsed, _ = generate_validated_json(
        client=client,
        model=model,
        provider=provider,
        system_prompt=(
            "You answer geography questions. Return only valid JSON that "
            "matches the supplied schema."
        ),
        user_prompt=(
            "What is the capital of France? Respond as a JSON object matching "
            f"this schema:\n{schema}"
        ),
        response_model=Capital,
    )

    assert parsed.country.strip().lower() == "france"
    assert parsed.capital.strip().lower() == "paris"
