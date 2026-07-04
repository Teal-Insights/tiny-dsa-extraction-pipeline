"""Tests for async LLM helpers and semaphore-limited concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import cast

import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from src.llm_json import generate_validated_json_async
from src.llm_providers import (
    LLM_MAX_CONCURRENT_ENV,
    ProviderConfig,
    build_async_client,
    build_async_client_if_configured,
    get_llm_semaphore,
    llm_max_concurrent,
    reset_llm_semaphore,
)

JSON_OBJECT_PROVIDER = ProviderConfig(
    name="fake",
    api_key_env="FAKE_API_KEY",
    base_url="https://example.invalid/",
    supports_structured_outputs=False,
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


class _FakeAsyncCompletions:
    def __init__(
        self,
        contents: list[str | None],
        *,
        delay: float = 0.0,
    ) -> None:
        self._contents = list(contents)
        self.calls: list[dict[str, object]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._delay = delay

    async def create(self, **kwargs: object) -> _FakeResponse:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            self.calls.append(kwargs)
            return _FakeResponse(self._contents.pop(0))
        finally:
            self.in_flight -= 1


class _FakeAsyncChat:
    def __init__(
        self,
        contents: list[str | None],
        *,
        delay: float = 0.0,
    ) -> None:
        self.completions = _FakeAsyncCompletions(contents, delay=delay)


class FakeAsyncClient:
    def __init__(
        self,
        contents: list[str | None],
        *,
        delay: float = 0.0,
    ) -> None:
        self.chat = _FakeAsyncChat(contents, delay=delay)


def _make_async(
    contents: list[str | None],
    *,
    delay: float = 0.0,
) -> tuple[AsyncOpenAI, FakeAsyncClient]:
    fake = FakeAsyncClient(contents, delay=delay)
    return cast(AsyncOpenAI, fake), fake


@pytest.fixture(autouse=True)
def _reset_semaphore() -> Iterator[None]:
    reset_llm_semaphore()
    yield
    reset_llm_semaphore()


def test_llm_max_concurrent_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_MAX_CONCURRENT_ENV, "7")
    assert llm_max_concurrent() == 7


def test_llm_max_concurrent_defaults_to_five(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LLM_MAX_CONCURRENT_ENV, raising=False)
    assert llm_max_concurrent() == 5


def test_build_async_client_uses_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client, config = build_async_client("gpt-5.5")
    assert config.api_key_env == "OPENAI_API_KEY"
    assert client.api_key == "test-key"
    assert client.base_url == "https://api.openai.com/v1/"


def test_build_async_client_if_configured_returns_none_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client, config = build_async_client_if_configured("deepseek-v4-pro")
    assert client is None
    assert config.api_key_env == "DEEPSEEK_API_KEY"


def test_generate_validated_json_async_returns_parsed_model() -> None:
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make_async([valid])

    parsed, content = asyncio.run(
        generate_validated_json_async(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
        )
    )

    assert parsed == _Sample(title="T", body="B")
    assert content == valid
    assert len(fake.chat.completions.calls) == 1


def test_generate_validated_json_async_retries_on_validation_failure() -> None:
    bad = '{"title": "T", "bodyy": "B"}'
    valid = '{"title": "T", "body": "B"}'
    client, fake = _make_async([bad, valid])

    parsed, _ = asyncio.run(
        generate_validated_json_async(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt="usr",
            response_model=_Sample,
        )
    )

    assert parsed.body == "B"
    assert len(fake.chat.completions.calls) == 2


def test_semaphore_limits_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_MAX_CONCURRENT_ENV, "2")
    reset_llm_semaphore()
    delay = 0.05
    contents = ['{"title": "A", "body": "B"}'] * 6
    client, fake = _make_async([*contents], delay=delay)
    semaphore = get_llm_semaphore()

    async def one_call(index: int) -> _Sample:
        parsed, _ = await generate_validated_json_async(
            client=client,
            model="m",
            provider=JSON_OBJECT_PROVIDER,
            system_prompt="sys",
            user_prompt=f"usr-{index}",
            response_model=_Sample,
            semaphore=semaphore,
        )
        return parsed

    async def run_all() -> None:
        await asyncio.gather(*(one_call(i) for i in range(6)))

    asyncio.run(run_all())

    assert fake.chat.completions.max_in_flight <= 2
    assert len(fake.chat.completions.calls) == 6
