"""Shared helper for schema-validated, self-healing LLM JSON calls.

All structured OpenAI calls in this project share the same contract: send a
system and user prompt, require a JSON object back, and validate it against a
Pydantic model (sometimes with extra semantic rules). When validation fails the
model can usually correct itself if told what went wrong, so this helper retries
with the validation error fed back as a follow-up turn.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from openai import Omit, OpenAI, omit
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import ResponseFormatJSONObject
from pydantic import BaseModel, ValidationError

from src.llm_providers import ProviderConfig

DEFAULT_MAX_ATTEMPTS = 3

T = TypeVar("T", bound=BaseModel)

LEGACY_OPENAI_PROVIDER = ProviderConfig(
    name="openai",
    api_key_env="OPENAI_API_KEY",
    base_url="https://api.openai.com/v1/",
    use_structured_outputs=False,
    supports_reasoning_effort=True,
)
"""Default for callers that have not adopted provider-based model switching.

Preserves the historical OpenAI JSON-object call shape so workflows that pass a
client without a ``provider`` keep behaving exactly as before."""


def _request_json(
    *,
    client: OpenAI,
    model: str,
    provider: ProviderConfig,
    messages: list[ChatCompletionMessageParam],
    response_model: type[T],
    reasoning_effort: ReasoningEffort,
) -> tuple[T | None, str]:
    """Perform one provider-appropriate JSON call.

    Returns the parsed model (only when the provider validates structurally on
    its side, i.e. structured outputs) and the raw JSON content string. The
    caller is responsible for parsing when the parsed model is ``None`` and for
    any semantic post-validation.
    """
    effort: ReasoningEffort | Omit = (
        reasoning_effort if provider.supports_reasoning_effort else omit
    )
    extra_body = provider.extra_body

    if provider.use_structured_outputs:
        parsed_response = client.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=response_model,
            reasoning_effort=effort,
            extra_body=extra_body,
        )
        message = parsed_response.choices[0].message
        if message.refusal:
            raise RuntimeError(f"LLM refused to respond: {message.refusal}")
        if message.content is None:
            raise RuntimeError("LLM returned empty response content")
        return message.parsed, message.content

    response_format: ResponseFormatJSONObject = {"type": "json_object"}
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False,
        response_format=response_format,
        reasoning_effort=effort,
        extra_body=extra_body,
    )
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("LLM returned empty response content")
    return None, content


def generate_validated_json(
    *,
    client: OpenAI,
    model: str,
    provider: ProviderConfig | None = None,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    post_validate: Callable[[T], T] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reasoning_effort: ReasoningEffort = "high",
) -> tuple[T, str]:
    """Call the LLM for JSON, validate it, and retry on validation failure.

    The model is asked for a JSON object using the strategy dictated by
    ``provider``: structured outputs for providers that validate the schema
    server-side (GPT), or JSON-object mode with the schema supplied in the
    prompt otherwise (GLM, DeepSeek). The response is parsed into
    ``response_model`` and optionally passed through ``post_validate`` for
    semantic checks or normalization. ``post_validate`` may transform the parsed
    model and may raise ``ValueError`` (or ``pydantic.ValidationError``) to
    reject a response. Any such failure is fed back to the model as a correction
    turn and the call is retried up to ``max_attempts`` times.

    Args:
        client: OpenAI-compatible client, e.g. from ``build_client``.
        model: Model name to call.
        provider: Provider metadata that selects the JSON call shape; pair it
            with ``client`` via ``build_client``. Defaults to
            ``LEGACY_OPENAI_PROVIDER`` (OpenAI JSON-object mode) when omitted.
        system_prompt: System role content.
        user_prompt: User role content (the task prompt).
        response_model: Pydantic model the JSON response must satisfy.
        post_validate: Optional hook applied to the parsed model; returns the
            possibly-transformed model and may raise to trigger a retry.
        max_attempts: Maximum number of model calls before giving up.
        reasoning_effort: Reasoning effort passed to providers that support it.

    Returns:
        A tuple of the validated model and the raw JSON content string from the
        successful attempt. Callers that cache a transformed model should cache
        ``parsed.model_dump_json()`` instead of the raw content.

    Raises:
        RuntimeError: If the model returns empty content, or if no attempt
            produces a valid response within ``max_attempts``.
    """
    resolved_provider = provider if provider is not None else LEGACY_OPENAI_PROVIDER
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error: Exception | None = None
    for _attempt in range(max_attempts):
        parsed_or_none, content = _request_json(
            client=client,
            model=model,
            provider=resolved_provider,
            messages=messages,
            response_model=response_model,
            reasoning_effort=reasoning_effort,
        )
        try:
            parsed = (
                parsed_or_none
                if parsed_or_none is not None
                else response_model.model_validate_json(content)
            )
            if post_validate is not None:
                parsed = post_validate(parsed)
        except (ValidationError, ValueError) as error:
            last_error = error
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed validation with these "
                        f"errors:\n{error}\n\n"
                        "Return corrected JSON matching the response schema "
                        "exactly. Use the exact field names from the schema, "
                        "include every required field, and satisfy all stated "
                        "constraints."
                    ),
                }
            )
            continue
        return parsed, content
    raise RuntimeError(
        f"LLM failed to return a valid {response_model.__name__} response after "
        f"{max_attempts} attempts"
    ) from last_error
