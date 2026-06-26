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

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import ResponseFormatJSONObject
from pydantic import BaseModel, ValidationError

DEFAULT_MAX_ATTEMPTS = 3

T = TypeVar("T", bound=BaseModel)


def generate_validated_json(
    *,
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    post_validate: Callable[[T], T] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reasoning_effort: ReasoningEffort = "high",
) -> tuple[T, str]:
    """Call the LLM for JSON, validate it, and retry on validation failure.

    The model is asked for a JSON object, parsed into ``response_model``, and
    optionally passed through ``post_validate`` for semantic checks or
    normalization. ``post_validate`` may transform the parsed model and may raise
    ``ValueError`` (or ``pydantic.ValidationError``) to reject a response. Any
    such failure is fed back to the model as a correction turn and the call is
    retried up to ``max_attempts`` times.

    Args:
        client: Configured OpenAI-compatible client.
        model: Model name to call.
        system_prompt: System role content.
        user_prompt: User role content (the task prompt).
        response_model: Pydantic model the JSON response must satisfy.
        post_validate: Optional hook applied to the parsed model; returns the
            possibly-transformed model and may raise to trigger a retry.
        max_attempts: Maximum number of model calls before giving up.
        reasoning_effort: Reasoning effort passed to the model.

    Returns:
        A tuple of the validated model and the raw JSON content string from the
        successful attempt. Callers that cache a transformed model should cache
        ``parsed.model_dump_json()`` instead of the raw content.

    Raises:
        RuntimeError: If the model returns empty content, or if no attempt
            produces a valid response within ``max_attempts``.
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response_format: ResponseFormatJSONObject = {"type": "json_object"}
    last_error: Exception | None = None
    for _attempt in range(max_attempts):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("LLM returned empty response content")
        try:
            parsed = response_model.model_validate_json(content)
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
