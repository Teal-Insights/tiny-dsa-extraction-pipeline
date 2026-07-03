"""Model-family routing for OpenAI-compatible providers.

Every model family in this project speaks the OpenAI wire protocol, but each
uses a different API key, base URL, and JSON-generation strategy. The model
name prefix selects the provider:

- ``gpt-*``      -> OpenAI, structured outputs (``response_format=<pydantic>``).
- ``glm-*``      -> Z.AI, JSON-object mode with the schema supplied in-prompt.
- ``deepseek-*`` -> DeepSeek, JSON-object mode with thinking disabled by default.

DeepSeek's thinking mode is opt-in through the ``DEEPSEEK_THINKING`` environment
variable: when it is truthy, thinking is enabled and reasoning effort is passed
through (DeepSeek only honours ``reasoning_effort`` in thinking mode). The toggle
is read when the provider config is resolved so a ``.env`` loaded after import is
still honoured.

``build_client`` returns both the configured client and the provider metadata
so callers can hand both to :func:`src.llm_json.generate_validated_json`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

DEFAULT_REQUEST_TIMEOUT = 120.0
"""Per-request timeout (seconds) so a stalled call fails loudly instead of
blocking on the SDK's 600s default."""

DEFAULT_MAX_RETRIES = 2
"""Transport-level retries the SDK performs (with backoff) on 429/5xx errors."""


@dataclass(frozen=True)
class ProviderConfig:
    """Static call-shape metadata for one OpenAI-compatible model family."""

    name: str
    api_key_env: str
    base_url: str
    supports_structured_outputs: bool
    supports_reasoning_effort: bool
    extra_body: dict[str, Any] | None = None


DEEPSEEK_THINKING_ENV = "DEEPSEEK_THINKING"
"""Environment variable that opts DeepSeek into thinking mode when truthy."""

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(env_var: str) -> bool:
    """Return whether ``env_var`` is set to a recognised truthy value."""
    return os.environ.get(env_var, "").strip().lower() in _TRUTHY


def _deepseek_config() -> ProviderConfig:
    """Build the DeepSeek provider config, honouring the thinking-mode toggle.

    Thinking is disabled by default. Setting ``DEEPSEEK_THINKING`` to a truthy
    value enables it and lets ``reasoning_effort`` through, which DeepSeek only
    applies in thinking mode.
    """
    thinking_enabled = _env_flag(DEEPSEEK_THINKING_ENV)
    return ProviderConfig(
        name="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        supports_structured_outputs=False,
        supports_reasoning_effort=thinking_enabled,
        extra_body={
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"}
        },
    )


def _providers() -> dict[str, ProviderConfig]:
    """Resolve the provider table, computing env-dependent configs on demand."""
    return {
        "gpt": ProviderConfig(
            name="openai",
            api_key_env="OPENAI_API_KEY",
            base_url="https://api.openai.com/v1/",
            supports_structured_outputs=True,
            supports_reasoning_effort=True,
        ),
        "glm": ProviderConfig(
            name="zai",
            api_key_env="ZAI_API_KEY",
            base_url="https://api.z.ai/api/paas/v4/",
            supports_structured_outputs=False,
            supports_reasoning_effort=False,
        ),
        "deepseek": _deepseek_config(),
    }


def model_from_env(env_var: str, override: str | None = None) -> str:
    """Return ``override`` if given, else the model named by ``env_var``.

    Model switching is configured through the environment (loaded from
    ``.env``). A missing variable with no explicit override is a hard error
    rather than a silent fallback to a default provider.
    """
    if override is not None:
        return override
    model = os.environ.get(env_var)
    if not model:
        raise RuntimeError(
            f"{env_var} must be set (e.g. in .env) or a model passed explicitly "
            "to select the provider"
        )
    return model


def provider_for_model(model: str) -> ProviderConfig:
    """Resolve the provider config for a model name by its family prefix."""
    prefix = model.split("-", 1)[0]
    providers = _providers()
    config = providers.get(prefix)
    if config is None:
        raise ValueError(
            f"Unsupported model {model!r}: name must start with one of "
            f"{sorted(providers)}"
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
    client = OpenAI(
        api_key=resolved_api_key,
        base_url=config.base_url,
        timeout=DEFAULT_REQUEST_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
    )
    return client, config


def build_client_if_configured(
    model: str,
    *,
    api_key: str | None = None,
) -> tuple[OpenAI | None, ProviderConfig]:
    """Like :func:`build_client`, but return ``(None, config)`` when no key.

    Useful for pipelines that can run from cache alone: callers get the provider
    config unconditionally and a client only when the provider's key is present,
    deferring the fail-fast to the point where an uncached call is attempted.
    """
    config = provider_for_model(model)
    resolved_api_key = api_key or os.environ.get(config.api_key_env)
    if not resolved_api_key:
        return None, config
    client = OpenAI(
        api_key=resolved_api_key,
        base_url=config.base_url,
        timeout=DEFAULT_REQUEST_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
    )
    return client, config
