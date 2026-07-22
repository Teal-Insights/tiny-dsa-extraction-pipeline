"""Tests for concurrent gather with per-success callbacks."""

from __future__ import annotations

import asyncio

import pytest

from src.async_gather import map_as_completed, run_map_as_completed


def test_map_as_completed_calls_on_success_before_failure() -> None:
    seen: list[str] = []

    async def _worker(name: str) -> tuple[str, str]:
        if name == "ok":
            await asyncio.sleep(0)
            return name, "value"
        await asyncio.sleep(0)
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError, match="boom"):
        asyncio.run(
            map_as_completed(
                ["ok", "bad"],
                _worker,
                on_success=lambda key, value: seen.append(f"{key}:{value}"),
            )
        )

    assert seen == ["ok:value"]


def test_run_map_as_completed_returns_all_successes() -> None:
    async def _worker(n: int) -> tuple[int, int]:
        await asyncio.sleep(0)
        return n, n * 2

    assert run_map_as_completed([1, 2, 3], _worker) == {1: 2, 2: 4, 3: 6}


def test_map_as_completed_soft_fail_continues_and_calls_on_error() -> None:
    successes: list[str] = []
    errors: list[str] = []

    async def _worker(name: str) -> tuple[str, str]:
        await asyncio.sleep(0)
        if name == "bad":
            raise ConnectionError("boom")
        return name, f"{name}-ok"

    result = asyncio.run(
        map_as_completed(
            ["a", "bad", "b"],
            _worker,
            on_success=lambda key, value: successes.append(f"{key}:{value}"),
            on_error=lambda item, exc: errors.append(f"{item}:{type(exc).__name__}"),
            raise_on_error=False,
        )
    )

    assert result == {"a": "a-ok", "b": "b-ok"}
    assert set(successes) == {"a:a-ok", "b:b-ok"}
    assert errors == ["bad:ConnectionError"]


def test_run_map_as_completed_soft_fail_returns_only_successes() -> None:
    async def _worker(n: int) -> tuple[int, int]:
        await asyncio.sleep(0)
        if n == 2:
            raise ValueError("nope")
        return n, n * 10

    assert run_map_as_completed(
        [1, 2, 3],
        _worker,
        raise_on_error=False,
    ) == {1: 10, 3: 30}
