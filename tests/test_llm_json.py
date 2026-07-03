from typing import cast

import pytest
from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from src.llm_json import generate_validated_json
from src.llm_providers import ProviderConfig

JSON_OBJECT_PROVIDER = ProviderConfig(
    name="fake",
    api_key_env="FAKE_API_KEY",
    base_url="https://example.invalid/",
    use_structured_outputs=False,
    supports_reasoning_effort=True,
)

STRUCTURED_PROVIDER = ProviderConfig(
    name="fake-structured",
    api_key_env="FAKE_API_KEY",
    base_url="https://example.invalid/",
    use_structured_outputs=True,
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
