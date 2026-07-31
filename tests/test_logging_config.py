import logging

import pytest

from src import logging_config
from src.logging_config import configure_logging


@pytest.fixture(autouse=True)
def reset_logging_config() -> None:
    logging_config._CONFIGURED = False
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)


def test_configure_logging_uses_info_by_default() -> None:
    configure_logging()

    assert logging_config._CONFIGURED is True
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert handler.formatter is not None
    assert handler.formatter._fmt == "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    assert handler.formatter.datefmt == "%H:%M:%S"


def test_configure_logging_respects_log_level_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    configure_logging()

    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_elevates_openai_and_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    configure_logging()

    assert logging.getLogger("openai").level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.DEBUG


def test_configure_logging_pins_httpcore_to_warning() -> None:
    configure_logging()

    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()
    first_handler = logging.getLogger().handlers[0]

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert root.handlers == [first_handler]
