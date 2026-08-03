"""Consolidated per-stage, per-span, and cache hit/miss timings for one pipeline run.

The pipeline already times individual stages (``StageTimer``) and each
``get_or_build_*`` cache already prints its own hit/miss line, but nothing
collects those numbers into one artifact. :class:`PipelineTimings` is threaded
through ``run_pipeline`` so a single run writes ``artifacts/stage-timings.json``
with the wall clock of every stage, the spans inside it, and the outcome of each
on-disk cache.

The file is rewritten after every stage record, so a run that dies mid-pipeline
still leaves the timings of the stages that completed — including the stage that
raised, when recorded via :meth:`PipelineTimings.stage`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ContextManager, Protocol

from src.pipeline_monitor import StageTimer

STAGE_TIMINGS_SCHEMA_VERSION = "1.0.0"
STAGE_TIMINGS_FILENAME = "stage-timings.json"

# The on-disk caches described in AGENTS.md, in the order a run reaches them.
CACHE_NAMES: tuple[str, ...] = (
    "dependency-graph",
    "bindings-validation",
    "series-resolution",
    "series-derived",
    "projection",
    "codegen",
    "clusters",
    "internals",
)

_UNOBSERVED_CACHE: dict[str, Any] = {
    "cache_hit": None,
    "elapsed_seconds": None,
    "cache_key": None,
}


class CacheResultLike(Protocol):
    """Read-only shape shared by every ``get_or_build_*`` cache result dataclass."""

    @property
    def cache_key(self) -> str: ...

    @property
    def cache_hit(self) -> bool: ...

    @property
    def elapsed_seconds(self) -> float: ...


def stage_timings_path(repo_root: Path) -> Path:
    """Return the consolidated stage-timings artifact path under ``repo_root``."""
    return repo_root / "artifacts" / STAGE_TIMINGS_FILENAME


@dataclass(frozen=True)
class CacheObservation:
    """One cache lookup outcome recorded at its pipeline call site."""

    name: str
    cache_hit: bool
    elapsed_seconds: float
    cache_key: str


@dataclass(frozen=True)
class StageRecord:
    """Wall clock for one pipeline stage plus the spans measured inside it."""

    name: str
    elapsed_seconds: float
    spans: Mapping[str, float]


@dataclass
class PipelineTimings:
    """Collect stage, span, and cache-outcome measurements for one pipeline run."""

    output_path: Path | None = None
    stages: list[StageRecord] = field(default_factory=list)
    caches: list[CacheObservation] = field(default_factory=list)

    def record_stage(
        self,
        name: str,
        *,
        elapsed_seconds: float,
        spans: Mapping[str, float] | None = None,
    ) -> None:
        """Record a stage whose wall clock the caller measured itself."""
        self.stages.append(
            StageRecord(
                name=name,
                elapsed_seconds=elapsed_seconds,
                spans=dict(spans) if spans is not None else {},
            )
        )
        self.flush()

    @contextmanager
    def stage(self, name: str) -> Iterator[StageTimer]:
        """Time ``name`` and capture every span recorded on the yielded timer.

        The stage is recorded even when the body raises, so a crashed run keeps
        the wall clock of the stage that failed.
        """
        timer = StageTimer()
        started = time.perf_counter()
        try:
            yield timer
        finally:
            self.record_stage(
                name,
                elapsed_seconds=time.perf_counter() - started,
                spans=timer.as_dict(),
            )

    def record_cache(
        self,
        name: str,
        *,
        cache_hit: bool,
        elapsed_seconds: float,
        cache_key: str,
    ) -> None:
        if name not in CACHE_NAMES:
            raise ValueError(
                f"unknown cache {name!r}; expected one of {list(CACHE_NAMES)}"
            )
        self.caches.append(
            CacheObservation(
                name=name,
                cache_hit=cache_hit,
                elapsed_seconds=elapsed_seconds,
                cache_key=cache_key,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        """Render the run as JSON-ready data with every cache name present."""
        observed = {
            observation.name: {
                "cache_hit": observation.cache_hit,
                "elapsed_seconds": round(observation.elapsed_seconds, 3),
                "cache_key": observation.cache_key,
            }
            # A repeated lookup of the same cache in one run keeps the last outcome.
            for observation in self.caches
        }
        return {
            "schema_version": STAGE_TIMINGS_SCHEMA_VERSION,
            "total_seconds": round(
                sum(record.elapsed_seconds for record in self.stages), 3
            ),
            "stages": [
                {
                    "name": record.name,
                    "elapsed_seconds": round(record.elapsed_seconds, 3),
                    "spans": {
                        span: round(seconds, 3)
                        for span, seconds in record.spans.items()
                    },
                }
                for record in self.stages
            ],
            "caches": {
                name: observed.get(name, dict(_UNOBSERVED_CACHE))
                for name in CACHE_NAMES
            },
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def flush(self) -> None:
        """Rewrite ``output_path`` when this run is backed by an artifact file."""
        if self.output_path is not None:
            self.write(self.output_path)


@contextmanager
def _detached_stage() -> Iterator[StageTimer]:
    yield StageTimer()


def stage_span(
    timings: PipelineTimings | None,
    name: str,
) -> ContextManager[StageTimer]:
    """Open stage ``name``, recording it only when ``timings`` was threaded in.

    Stage entry points are also callable standalone (scripts, tests), where
    there is no run to attribute the stage to; the detached timer keeps those
    call sites free of ``if timings is not None`` branches.
    """
    if timings is None:
        return _detached_stage()
    return timings.stage(name)


def record_cache_result(
    timings: PipelineTimings | None,
    name: str,
    result: CacheResultLike,
) -> None:
    """Record a ``get_or_build_*`` outcome when a run is collecting timings."""
    # Return before touching ``result`` so a detached run never depends on the
    # result object's shape.
    if timings is None:
        return
    record_cache_outcome(
        timings,
        name,
        cache_hit=result.cache_hit,
        elapsed_seconds=result.elapsed_seconds,
        cache_key=result.cache_key,
    )


def record_cache_outcome(
    timings: PipelineTimings | None,
    name: str,
    *,
    cache_hit: bool,
    elapsed_seconds: float,
    cache_key: str,
) -> None:
    """Record a cache outcome resolved without a ``get_or_build_*`` result object.

    The refactored-internals cache is resolved by direct load/save calls rather
    than a single ``get_or_build_*`` helper, so its hit/miss has to be reported
    from each of the refactor stage's resolution points.
    """
    if timings is None:
        return
    timings.record_cache(
        name,
        cache_hit=cache_hit,
        elapsed_seconds=elapsed_seconds,
        cache_key=cache_key,
    )
