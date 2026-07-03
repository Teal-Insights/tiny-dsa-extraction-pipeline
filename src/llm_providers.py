"""Model-family routing for OpenAI-compatible providers.

Every model family in this project speaks the OpenAI wire protocol, but each
uses a different API key, base URL, and JSON-generation strategy. The model
name prefix selects the provider:

- ``gpt-*``      -> OpenAI, structured outputs (``response_format=<pydantic>``).
- ``glm-*``      -> Z.AI, JSON-object mode with the schema supplied in-prompt.
- ``deepseek-*`` -> DeepSeek, JSON-object mode with thinking disabled.

``build_client`` returns both the configured client and the provider metadata
so callers can hand both to :func:`src.llm_json.generate_validated_json`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import OpenAI


@dataclass(frozen=True)
class ProviderConfig:
    """Static call-shape metadata for one OpenAI-compatible model family."""

    name: str
    api_key_env: str
    base_url: str
    use_structured_outputs: bool
    supports_reasoning_effort: bool
    extra_body: dict[str, Any] | None = None


PROVIDERS: dict[str, ProviderConfig] = {
    "gpt": ProviderConfig(
        name="openai",
        api_key_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1/",
        use_structured_outputs=True,
        supports_reasoning_effort=True,
    ),
    "glm": ProviderConfig(
        name="zai",
        api_key_env="ZAI_API_KEY",
        base_url="https://api.z.ai/api/paas/v4/",
        use_structured_outputs=False,
        supports_reasoning_effort=False,
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        use_structured_outputs=False,
        supports_reasoning_effort=False,
        extra_body={"thinking": {"type": "disabled"}},
    ),
}


def provider_for_model(model: str) -> ProviderConfig:
    """Resolve the provider config for a model name by its family prefix."""
    prefix = model.split("-", 1)[0]
    config = PROVIDERS.get(prefix)
    if config is None:
        raise ValueError(
            f"Unsupported model {model!r}: name must start with one of "
            f"{sorted(PROVIDERS)}"
        )
    return config


def build_client(
    model: str,
    *,
    api_key: str | None = None,
) -> tuple[OpenAI, ProviderConfig]:
    """Build an OpenAI-compatible client for ``model`` and its provider config.

    The API key is taken from ``api_key`` when given, otherwise from the
    provider's environment variable. Raises ``RuntimeError`` when no key is
    available so uncached generation fails fast and loudly.
    """
    config = provider_for_model(model)
    resolved_api_key = api_key or os.environ.get(config.api_key_env)
    if not resolved_api_key:
        raise RuntimeError(
            f"{config.api_key_env} is required to call {config.name} model {model!r}"
        )
    client = OpenAI(api_key=resolved_api_key, base_url=config.base_url)
    return client, config
