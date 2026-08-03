from collections.abc import Callable
from typing import cast
from unittest.mock import patch

import pytest
from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from src.llm_json import (
    ValidatedJsonFailure,
    ValidatedJsonTimeout,
    generate_validated_json,
)
from src.llm_providers import ProviderConfig

JSON_OBJECT_PROVIDER = ProviderConfig(
    name="fake",
    api_key_env="FAKE_API_KEY",
    base_url="https://example.invalid/",
    supports_structured_outputs=False,
    supports_reasoning_effort=True,
)

STRUCTURED_PROVIDER = ProviderConfig(
    name="fake-structured",
    api_key_env="FAKE_API_KEY",
    base_url="https://example.invalid/",
    supports_structured_outputs=True,
    supports_reasoning_effort=True,
)


class _Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    body: str


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
    def __init__(
        self,
        contents: list[str | None],
        *,
        after_create: Callable[[], None] | None = None,
    ) -> None:
        self._contents = list(contents)
        self.calls: list[dict[str, object]] = []
        self._after_create = after_create

    def create(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(kwargs)
        response = _FakeResponse(self._contents.pop(0))
        if self._after_create is not None:
            self._after_create()
        return response


class _FakeChat:
    def __init__(
        self,
        contents: list[str | None],
        *,
        after_create: Callable[[], None] | None = None,
    ) -> None:
        self.completions = _FakeCompletions(contents, after_create=after_create)


class FakeClient:
    def __init__(
        self,
        contents: list[str | None],
        *,
        after_create: Callable[[], None] | None = None,
    ) -> None:
        self.chat = _FakeChat(contents, after_create=after_create)


def _make(
    contents: list[str | None],
    *,
    after_create: Callable[[], None] | None = None,
) -> tuple[OpenAI, FakeClient]:
    fake = FakeClient(contents, after_create=after_create)
    return cast(OpenAI, fake), fake


def test_returns_parsed_and_raw_content_on_first_success() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make([valid])

    parsed, content = generate_validated_json(
        client=client,
        model="m",
        provider=JSON_OBJECT_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
    )

    assert parsed == _Sample(title="T", body="B")
    assert content == valid
    assert len(fake.chat.completions.calls) == 1
    assert fake.chat.completions.calls[0]["reasoning_effort"] == "high"
    assert fake.chat.completions.calls[0]["extra_body"] is None


def test_retries_with_error_feedback_then_succeeds() -> None:
    bad = '{"title": "T", "bodyy": "B"}'
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make([bad, valid])

    parsed, _ = generate_validated_json(
        client=client,
        model="m",
        provider=JSON_OBJECT_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
    )

    assert parsed.body == "B"
    calls = fake.chat.completions.calls
    assert len(calls) == 2
    second_messages = cast(list[dict[str, str]], calls[1]["messages"])
    assert second_messages[-2] == {"role": "assistant", "content": bad}
    assert second_messages[-1]["role"] == "user"
    assert "failed validation" in second_messages[-1]["content"]


def test_post_validate_xl_index_ref_hint_appears_in_reprompt() -> None:
    """Static xl_index_ref(xl_range(...)) rejection must surface in the retry prompt."""
    from src.internals_refactor import XL_INDEX_REF_OF_XL_RANGE_HINT

    first = '{"title": "bad", "body": "B"}'
    second = '{"title": "ok", "body": "B"}'
    client, fake = _make([first, second])

    def reject_once(parsed: _Sample) -> _Sample:
        if parsed.title == "bad":
            raise ValueError(XL_INDEX_REF_OF_XL_RANGE_HINT)
        return parsed

    parsed, _ = generate_validated_json(
        client=client,
        model="m",
        provider=JSON_OBJECT_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
        post_validate=reject_once,
    )

    assert parsed.title == "ok"
    retry_user = cast(list[dict[str, str]], fake.chat.completions.calls[1]["messages"])[
        -1
    ]["content"]
    assert XL_INDEX_REF_OF_XL_RANGE_HINT in retry_user
    assert "do not pass xl_range" in retry_user


def test_post_validate_failure_triggers_retry() -> None:
    first = '{"title": "bad", "body": "B"}'
    second = '{"title": "ok", "body": "B"}'
    client, fake = _make([first, second])

    def reject_bad_title(parsed: _Sample) -> _Sample:
        if parsed.title == "bad":
            raise ValueError("title must not be 'bad'")
        return parsed

    parsed, _ = generate_validated_json(
        client=client,
        model="m",
        provider=JSON_OBJECT_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
        post_validate=reject_bad_title,
    )

    assert parsed.title == "ok"
    assert len(fake.chat.completions.calls) == 2


def test_post_validate_runtime_error_does_not_retry() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make([valid, valid])

    def abort(_parsed: _Sample) -> _Sample:
        raise RuntimeError("declared abort")

    with pytest.raises(RuntimeError, match="declared abort"):
        generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
            post_validate=abort,
            max_attempts=3,
        )

    assert len(fake.chat.completions.calls) == 1


def test_post_validate_can_transform_result() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, _ = _make([valid])

    def upper_title(parsed: _Sample) -> _Sample:
        return parsed.model_copy(update={"title": parsed.title.upper()})

    parsed, _ = generate_validated_json(
        client=client,
        model="m",
        provider=JSON_OBJECT_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
        post_validate=upper_title,
    )

    assert parsed.title == "T"


def test_raises_after_exhausting_attempts() -> None:
    bad = '{"title": "T", "bodyy": "B"}'
    client, fake = _make([bad, bad])

    with pytest.raises(RuntimeError) as excinfo:
        generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
            max_attempts=2,
        )

    assert "after 2 attempts" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None
    assert len(fake.chat.completions.calls) == 2


def test_exhausted_attempts_raise_validated_json_failure_with_history() -> None:
    first = '{"title": "T", "bodyy": "first"}'
    second = '{"title": "T", "bodyy": "second"}'
    third = '{"title": "T", "bodyy": "third"}'
    client, fake = _make([first, second, third])

    with pytest.raises(ValidatedJsonFailure) as excinfo:
        generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
            max_attempts=3,
        )

    failure = excinfo.value
    assert isinstance(failure, RuntimeError)
    assert len(failure.attempts) == 3
    assert [record.raw_content for record in failure.attempts] == [
        first,
        second,
        third,
    ]
    assert all(record.error for record in failure.attempts)
    assert failure.attempts[0].attempt == 1
    assert failure.attempts[2].attempt == 3

    roles = [message["role"] for message in failure.messages]
    assert roles[0] == "system"
    assert roles[1] == "user"
    assert failure.messages[1]["content"] == "usr"
    # Each failed attempt appends assistant content + a correction user turn.
    assert roles[2:] == ["assistant", "user", "assistant", "user", "assistant", "user"]
    assert failure.messages[2]["content"] == first
    assert "failed validation" in str(failure.messages[3]["content"])
    assert failure.messages[4]["content"] == second
    assert failure.messages[6]["content"] == third
    assert len(fake.chat.completions.calls) == 3


def test_empty_content_raises_without_retry() -> None:
    client, fake = _make([None])

    with pytest.raises(RuntimeError, match="empty response content"):
        generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
        )

    assert len(fake.chat.completions.calls) == 1


class _FakeParsedMessage:
    def __init__(self, parsed: _Sample | None, content: str | None) -> None:
        self.parsed = parsed
        self.content = content
        self.refusal: str | None = None


class _FakeParsedChoice:
    def __init__(self, parsed: _Sample | None, content: str | None) -> None:
        self.message = _FakeParsedMessage(parsed, content)


class _FakeParsedResponse:
    def __init__(self, parsed: _Sample | None, content: str | None) -> None:
        self.choices = [_FakeParsedChoice(parsed, content)]


class _FakeParseCompletions:
    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> _FakeParsedResponse:
        self.calls.append(kwargs)
        content = self._contents.pop(0)
        return _FakeParsedResponse(_Sample.model_validate_json(content), content)


class _FakeParseChat:
    def __init__(self, contents: list[str]) -> None:
        self.completions = _FakeParseCompletions(contents)


class FakeParseClient:
    def __init__(self, contents: list[str]) -> None:
        self.chat = _FakeParseChat(contents)


def test_inferred_openai_provider_uses_structured_parse() -> None:
    valid = '{"title": "T", "body": "B"}'
    fake = FakeParseClient([valid])

    parsed, content = generate_validated_json(
        client=cast(OpenAI, fake),
        model="gpt-5.5",
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
    )

    assert parsed == _Sample(title="T", body="B")
    assert content == valid
    assert len(fake.chat.completions.calls) == 1
    assert fake.chat.completions.calls[0]["response_format"] is _Sample


def test_structured_provider_uses_parse_and_returns_parsed_model() -> None:
    valid = '{"title": "T", "body": "B"}'
    fake = FakeParseClient([valid])

    parsed, content = generate_validated_json(
        client=cast(OpenAI, fake),
        model="m",
        provider=STRUCTURED_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
    )

    assert parsed == _Sample(title="T", body="B")
    assert content == valid
    calls = fake.chat.completions.calls
    assert len(calls) == 1
    assert calls[0]["response_format"] is _Sample
    assert calls[0]["reasoning_effort"] == "high"


def test_structured_provider_retries_on_post_validate_failure() -> None:
    first = '{"title": "bad", "body": "B"}'
    second = '{"title": "ok", "body": "B"}'
    fake = FakeParseClient([first, second])

    def reject_bad_title(parsed: _Sample) -> _Sample:
        if parsed.title == "bad":
            raise ValueError("title must not be 'bad'")
        return parsed

    parsed, _ = generate_validated_json(
        client=cast(OpenAI, fake),
        model="m",
        provider=STRUCTURED_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
        post_validate=reject_bad_title,
    )

    assert parsed.title == "ok"
    assert len(fake.chat.completions.calls) == 2


def test_structured_opt_out_uses_json_object_on_structured_provider() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make([valid])

    parsed, content = generate_validated_json(
        client=client,
        model="m",
        provider=STRUCTURED_PROVIDER,
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
        structured=False,
    )

    assert parsed == _Sample(title="T", body="B")
    assert content == valid
    calls = fake.chat.completions.calls
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_deadline_exceeded_before_next_attempt_raises_timeout() -> None:
    bad = '{"title": "T", "bodyy": "B"}'
    clock = {"now": 0.0}

    def advance_past_deadline() -> None:
        clock["now"] = 10.0

    client, fake = _make([bad, bad, bad], after_create=advance_past_deadline)

    with (
        patch("src.llm_json.time.perf_counter", side_effect=lambda: clock["now"]),
        pytest.raises(ValidatedJsonTimeout, match="deadline"),
    ):
        generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
            max_attempts=3,
            deadline_seconds=5.0,
        )

    assert len(fake.chat.completions.calls) == 1


def test_deadline_allows_retries_while_under_budget() -> None:
    bad = '{"title": "T", "bodyy": "B"}'
    valid = '{"title": "T", "body": "B"}'
    clock = {"now": 0.0}

    def advance_one_second() -> None:
        clock["now"] += 1.0

    client, fake = _make([bad, valid], after_create=advance_one_second)

    with patch("src.llm_json.time.perf_counter", side_effect=lambda: clock["now"]):
        parsed, _ = generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
            max_attempts=3,
            deadline_seconds=5.0,
        )

    assert parsed == _Sample(title="T", body="B")
    assert len(fake.chat.completions.calls) == 2


def test_attempt_start_is_logged_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    valid = '{"title": "T", "body": "B"}'
    client, _ = _make([valid])

    with caplog.at_level("INFO", logger="src.llm_json"):
        generate_validated_json(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
        )

    assert any(
        "requesting _Sample from m (attempt 1/3)" in r.message for r in caplog.records
    )
