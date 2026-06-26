from typing import cast

import pytest
from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from src.llm_json import generate_validated_json


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
    def __init__(self, contents: list[str | None]) -> None:
        self._contents = list(contents)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(self._contents.pop(0))


class _FakeChat:
    def __init__(self, contents: list[str | None]) -> None:
        self.completions = _FakeCompletions(contents)


class FakeClient:
    def __init__(self, contents: list[str | None]) -> None:
        self.chat = _FakeChat(contents)


def _make(contents: list[str | None]) -> tuple[OpenAI, FakeClient]:
    fake = FakeClient(contents)
    return cast(OpenAI, fake), fake


def test_returns_parsed_and_raw_content_on_first_success() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make([valid])

    parsed, content = generate_validated_json(
        client=client,
        model="m",
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
    )

    assert parsed == _Sample(title="T", body="B")
    assert content == valid
    assert len(fake.chat.completions.calls) == 1
    assert fake.chat.completions.calls[0]["reasoning_effort"] == "high"
    assert "extra_body" not in fake.chat.completions.calls[0]


def test_retries_with_error_feedback_then_succeeds() -> None:
    bad = '{"title": "T", "bodyy": "B"}'
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make([bad, valid])

    parsed, _ = generate_validated_json(
        client=client,
        model="m",
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
        system_prompt="sys",
        user_prompt="usr",
        response_model=_Sample,
        post_validate=reject_bad_title,
    )

    assert parsed.title == "ok"
    assert len(fake.chat.completions.calls) == 2


def test_post_validate_can_transform_result() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, _ = _make([valid])

    def upper_title(parsed: _Sample) -> _Sample:
        return parsed.model_copy(update={"title": parsed.title.upper()})

    parsed, _ = generate_validated_json(
        client=client,
        model="m",
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
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
            max_attempts=2,
        )

    assert "after 2 attempts" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None
    assert len(fake.chat.completions.calls) == 2


def test_empty_content_raises_without_retry() -> None:
    client, fake = _make([None])

    with pytest.raises(RuntimeError, match="empty response content"):
        generate_validated_json(
            client=client,
            model="m",
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
        )

    assert len(fake.chat.completions.calls) == 1
