"""Unit tests for provider routing and client construction."""

from __future__ import annotations

import pytest

from src.llm_providers import (
    DEEPSEEK_THINKING_ENV,
    build_async_client,
    build_async_client_if_configured,
    build_client,
    build_client_if_configured,
    model_from_env,
    provider_for_model,
)


def test_model_from_env_returns_override() -> None:
    assert model_from_env("MISSING_VAR", "gpt-5.5") == "gpt-5.5"


def test_model_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_MODEL", "glm-5.2")
    assert model_from_env("TEST_MODEL") == "glm-5.2"


def test_model_from_env_uses_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_MODEL", raising=False)
    assert model_from_env("TEST_MODEL") == "gpt-5.5"


def test_model_from_env_requires_explicit_value_when_default_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="TEST_MODEL must be set"):
        model_from_env("TEST_MODEL", default=None)


@pytest.mark.parametrize(
    ("model", "api_key_env", "supports_structured"),
    [
        ("gpt-5.5", "OPENAI_API_KEY", True),
        ("glm-5.2", "ZAI_API_KEY", False),
        ("deepseek-v4-pro", "DEEPSEEK_API_KEY", False),
    ],
)
def test_provider_for_model(
    model: str,
    api_key_env: str,
    supports_structured: bool,
) -> None:
    config = provider_for_model(model)
    assert config.api_key_env == api_key_env
    assert config.supports_structured_outputs is supports_structured


def test_provider_for_model_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError, match="Unsupported model 'claude-3'"):
        provider_for_model("claude-3")


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


def test_build_client_uses_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client, config = build_client("gpt-5.5")
    assert config.api_key_env == "OPENAI_API_KEY"
    assert client.api_key == "test-key"
    assert client.base_url == "https://api.openai.com/v1/"


def test_build_client_prefers_explicit_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client, _ = build_client("gpt-5.5", api_key="override-key")
    assert client.api_key == "override-key"


def test_build_client_fails_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ZAI_API_KEY is required"):
        build_client("glm-5.2")


def test_build_client_if_configured_returns_none_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client, config = build_client_if_configured("deepseek-v4-pro")
    assert client is None
    assert config.api_key_env == "DEEPSEEK_API_KEY"


def test_build_client_if_configured_returns_client_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    client, config = build_client_if_configured("deepseek-v4-pro")
    assert client is not None
    assert config.name == "deepseek"
    assert client.api_key == "ds-key"


def test_deepseek_thinking_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEEPSEEK_THINKING_ENV, raising=False)
    config = provider_for_model("deepseek-v4-pro")
    assert config.supports_reasoning_effort is False
    assert config.extra_body == {"thinking": {"type": "disabled"}}


def test_deepseek_thinking_enabled_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEEPSEEK_THINKING_ENV, "true")
    config = provider_for_model("deepseek-v4-pro")
    assert config.supports_reasoning_effort is True
    assert config.extra_body == {"thinking": {"type": "enabled"}}
