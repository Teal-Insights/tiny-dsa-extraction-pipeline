"""Gather concurrent awaitables, delivering successes as they complete."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, TypeVar

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


async def map_as_completed[T, K, V](
    items: Sequence[T],
    worker: Callable[[T], Coroutine[Any, Any, tuple[K, V]]],
    *,
    on_success: Callable[[K, V], None] | None = None,
    on_error: Callable[[T, BaseException], None] | None = None,
    raise_on_error: bool = True,
) -> dict[K, V]:
    """Run ``worker`` over ``items`` concurrently; call ``on_success`` per result.

    When ``raise_on_error`` is true (default) and a worker fails, remaining
    in-flight tasks are cancelled after any already-finished successes have
    been recorded (so callers can persist partial progress before the
    exception propagates).

    When ``raise_on_error`` is false, failures invoke ``on_error`` (if given)
    with the original item and exception, siblings keep running, and only
    successes are returned.
    """
    if not items:
        return {}

    tasks = {asyncio.create_task(worker(item)): item for item in items}
    results: dict[K, V] = {}
    pending = set(tasks)

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        batch_error: BaseException | None = None
        for task in done:
            item = tasks[task]
            try:
                key, value = task.result()
            except BaseException as exc:  # noqa: BLE001
                if on_error is not None:
                    on_error(item, exc)
                if raise_on_error and batch_error is None:
                    batch_error = exc
                continue
            results[key] = value
            if on_success is not None:
                on_success(key, value)
        if batch_error is not None:
            for leftover in pending:
                leftover.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise batch_error
    return results


def run_map_as_completed[T, K, V](
    items: Sequence[T],
    worker: Callable[[T], Coroutine[Any, Any, tuple[K, V]]],
    *,
    on_success: Callable[[K, V], None] | None = None,
    on_error: Callable[[T, BaseException], None] | None = None,
    raise_on_error: bool = True,
) -> dict[K, V]:
    """Sync wrapper around :func:`map_as_completed`."""
    return asyncio.run(
        map_as_completed(
            items,
            worker,
            on_success=on_success,
            on_error=on_error,
            raise_on_error=raise_on_error,
        )
    )
