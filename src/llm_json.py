"""Shared helper for schema-validated, self-healing LLM JSON calls.

All structured OpenAI calls in this project share the same contract: send a
system and user prompt, require a JSON object back, and validate it against a
Pydantic model (sometimes with extra semantic rules). When validation fails the
model can usually correct itself if told what went wrong, so this helper retries
with the validation error fed back as a follow-up turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from openai import AsyncOpenAI, Omit, OpenAI, OpenAIError, omit
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import ResponseFormatJSONObject
from pydantic import BaseModel, ValidationError

from src.llm_providers import ProviderConfig, get_llm_semaphore, provider_for_model

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ValidationAttemptRecord:
    """One validation failure inside :func:`generate_validated_json`."""

    attempt: int
    raw_content: str
    error: str


class ValidatedJsonFailure(RuntimeError):
    """Raised when every validation attempt fails.

    Carries the full chat ``messages`` list (system + initial user prompt + each
    assistant/correction turn) and per-attempt raw content / validation errors so
    callers can dump the whole retry conversation for offline diagnosis.
    """

    def __init__(
        self,
        message: str,
        *,
        messages: Sequence[ChatCompletionMessageParam],
        attempts: Sequence[ValidationAttemptRecord],
        last_error: Exception | None,
    ) -> None:
        super().__init__(message)
        self.messages = list(messages)
        self.attempts = list(attempts)
        self.last_error = last_error


class ValidatedJsonTimeout(RuntimeError):
    """Raised when a wall-clock deadline elapses before a valid response."""

    def __init__(
        self,
        message: str,
        *,
        elapsed_seconds: float,
        deadline_seconds: float,
        attempts_completed: int,
    ) -> None:
        super().__init__(message)
        self.elapsed_seconds = elapsed_seconds
        self.deadline_seconds = deadline_seconds
        self.attempts_completed = attempts_completed


def _validation_feedback_message(error: Exception) -> ChatCompletionMessageParam:
    return {
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


def _request_json(
    *,
    client: OpenAI,
    model: str,
    provider: ProviderConfig,
    messages: list[ChatCompletionMessageParam],
    response_model: type[T],
    reasoning_effort: ReasoningEffort,
    use_structured_outputs: bool,
) -> tuple[T | None, str]:
    """Perform one provider-appropriate JSON call.

    Returns the parsed model (only for the structured-outputs path, where the
    provider validates the schema server-side) and the raw JSON content string.
    The caller is responsible for parsing when the parsed model is ``None`` and
    for any semantic post-validation.
    """
    effort: ReasoningEffort | Omit = (
        reasoning_effort if provider.supports_reasoning_effort else omit
    )
    extra_body = provider.extra_body

    if use_structured_outputs:
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


async def _request_json_async(
    *,
    client: AsyncOpenAI,
    model: str,
    provider: ProviderConfig,
    messages: list[ChatCompletionMessageParam],
    response_model: type[T],
    reasoning_effort: ReasoningEffort,
    use_structured_outputs: bool,
) -> tuple[T | None, str]:
    """Perform one async provider-appropriate JSON call."""
    effort: ReasoningEffort | Omit = (
        reasoning_effort if provider.supports_reasoning_effort else omit
    )
    extra_body = provider.extra_body

    if use_structured_outputs:
        parsed_response = await client.chat.completions.parse(
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
    response = await client.chat.completions.create(
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
    structured: bool = True,
    deadline_seconds: float | None = None,
) -> tuple[T, str]:
    """Call the LLM for JSON, validate it, and retry on validation failure.

    The JSON call shape is chosen from the provider's capability and the
    caller's intent: structured outputs are used only when the provider
    supports them (GPT) and ``structured`` is left ``True``. Otherwise the model
    is asked for a JSON object with the schema supplied in the prompt (GLM,
    DeepSeek, or any response model whose schema is not compatible with strict
    structured outputs). The response is parsed into ``response_model`` and
    optionally passed through ``post_validate`` for semantic checks or
    normalization. ``post_validate`` may transform the parsed model and may
    raise ``ValueError`` (or ``pydantic.ValidationError``) to reject a response.
    Any such failure is fed back to the model as a correction turn and the call
    is retried up to ``max_attempts`` times.

    Args:
        client: OpenAI-compatible client, e.g. from ``build_client``.
        model: Model name to call.
        provider: Provider metadata that selects the JSON call shape; pair it
            with ``client`` via ``build_client``. When omitted, inferred from
            ``model`` via :func:`src.llm_providers.provider_for_model`.
        system_prompt: System role content.
        user_prompt: User role content (the task prompt).
        response_model: Pydantic model the JSON response must satisfy.
        post_validate: Optional hook applied to the parsed model; returns the
            possibly-transformed model and may raise to trigger a retry.
        max_attempts: Maximum number of model calls before giving up.
        reasoning_effort: Reasoning effort passed to providers that support it.
        structured: Opt out of structured outputs by passing ``False``; required
            when ``response_model`` has a schema that strict structured outputs
            reject (e.g. open-ended ``dict`` fields). Ignored for providers that
            do not support structured outputs.
        deadline_seconds: Optional wall-clock budget for the whole retry loop.
            When set, a new attempt is not started once elapsed time meets or
            exceeds this budget; raises :class:`ValidatedJsonTimeout`.

    Returns:
        A tuple of the validated model and the raw JSON content string from the
        successful attempt. Callers that cache a transformed model should cache
        ``parsed.model_dump_json()`` instead of the raw content.

    Raises:
        RuntimeError: If the model returns empty content.
        ValidatedJsonTimeout: If ``deadline_seconds`` elapses before a valid
            response is produced.
        ValidatedJsonFailure: If no attempt produces a valid response within
            ``max_attempts``. Subclasses ``RuntimeError`` and includes the full
            conversation and per-attempt validation errors.
    """
    resolved_provider = provider if provider is not None else provider_for_model(model)
    use_structured_outputs = (
        structured and resolved_provider.supports_structured_outputs
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error: Exception | None = None
    attempt_records: list[ValidationAttemptRecord] = []
    started = time.perf_counter()
    for attempt in range(max_attempts):
        if deadline_seconds is not None:
            elapsed = time.perf_counter() - started
            if elapsed >= deadline_seconds:
                raise ValidatedJsonTimeout(
                    f"LLM deadline of {deadline_seconds:.1f}s exceeded after "
                    f"{elapsed:.1f}s while validating {response_model.__name__} "
                    f"({attempt} attempts completed)",
                    elapsed_seconds=elapsed,
                    deadline_seconds=deadline_seconds,
                    attempts_completed=attempt,
                )
        logger.info(
            "requesting %s from %s (attempt %d/%d)",
            response_model.__name__,
            model,
            attempt + 1,
            max_attempts,
        )
        try:
            parsed_or_none, content = _request_json(
                client=client,
                model=model,
                provider=resolved_provider,
                messages=messages,
                response_model=response_model,
                reasoning_effort=reasoning_effort,
                use_structured_outputs=use_structured_outputs,
            )
        except OpenAIError as error:
            logger.error(
                "%s request to %s failed (attempt %d/%d): %s",
                response_model.__name__,
                model,
                attempt + 1,
                max_attempts,
                error,
            )
            raise
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
            attempt_records.append(
                ValidationAttemptRecord(
                    attempt=attempt + 1,
                    raw_content=content,
                    error=str(error),
                )
            )
            logger.warning(
                "%s failed validation (attempt %d/%d), re-prompting: %s",
                response_model.__name__,
                attempt + 1,
                max_attempts,
                error,
            )
            messages.append({"role": "assistant", "content": content})
            messages.append(_validation_feedback_message(error))
            continue
        logger.info(
            "%s validated on attempt %d/%d",
            response_model.__name__,
            attempt + 1,
            max_attempts,
        )
        return parsed, content
    raise ValidatedJsonFailure(
        f"LLM failed to return a valid {response_model.__name__} response after "
        f"{max_attempts} attempts",
        messages=messages,
        attempts=attempt_records,
        last_error=last_error,
    ) from last_error


async def generate_validated_json_async(
    *,
    client: AsyncOpenAI,
    model: str,
    provider: ProviderConfig | None = None,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    post_validate: Callable[[T], T] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reasoning_effort: ReasoningEffort = "high",
    structured: bool = True,
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[T, str]:
    """Async counterpart to :func:`generate_validated_json`.

    When ``semaphore`` is omitted, the process-wide limit from
    :func:`src.llm_providers.get_llm_semaphore` is applied around each model
    request. Pass an explicit semaphore to share a custom limit across tasks.
    """
    resolved_provider = provider if provider is not None else provider_for_model(model)
    use_structured_outputs = (
        structured and resolved_provider.supports_structured_outputs
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    limiter = semaphore if semaphore is not None else get_llm_semaphore()
    last_error: Exception | None = None
    attempt_records: list[ValidationAttemptRecord] = []
    for attempt in range(max_attempts):
        logger.debug(
            "requesting %s from %s (attempt %d/%d, async)",
            response_model.__name__,
            model,
            attempt + 1,
            max_attempts,
        )
        try:
            async with limiter:
                parsed_or_none, content = await _request_json_async(
                    client=client,
                    model=model,
                    provider=resolved_provider,
                    messages=messages,
                    response_model=response_model,
                    reasoning_effort=reasoning_effort,
                    use_structured_outputs=use_structured_outputs,
                )
        except OpenAIError as error:
            logger.error(
                "%s request to %s failed (attempt %d/%d, async): %s",
                response_model.__name__,
                model,
                attempt + 1,
                max_attempts,
                error,
            )
            raise
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
            attempt_records.append(
                ValidationAttemptRecord(
                    attempt=attempt + 1,
                    raw_content=content,
                    error=str(error),
                )
            )
            logger.warning(
                "%s failed validation (attempt %d/%d, async), re-prompting: %s",
                response_model.__name__,
                attempt + 1,
                max_attempts,
                error,
            )
            messages.append({"role": "assistant", "content": content})
            messages.append(_validation_feedback_message(error))
            continue
        logger.debug(
            "%s validated on attempt %d/%d (async)",
            response_model.__name__,
            attempt + 1,
            max_attempts,
        )
        return parsed, content
    raise ValidatedJsonFailure(
        f"LLM failed to return a valid {response_model.__name__} response after "
        f"{max_attempts} attempts",
        messages=messages,
        attempts=attempt_records,
        last_error=last_error,
    ) from last_error
