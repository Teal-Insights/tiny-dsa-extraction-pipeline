"""Timing, stall detection, and optional profiling for long pipeline stages."""

from __future__ import annotations

import cProfile
import faulthandler
import os
import pstats
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import IO

from src.env_utils import env_flag, env_float


def dump_thread_stacks(
    file: IO[str],
    *,
    header: str | None = None,
) -> str | None:
    """Write all thread stacks and return a one-line summary of the main thread."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    file.write(f"\n--- thread stacks @ {stamp}")
    if header:
        file.write(f" ({header})")
    file.write(" ---\n")
    frames = sys._current_frames()
    main_summary: str | None = None
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None:
            continue
        file.write(f"\nThread {thread.name!r} (id={thread.ident}):\n")
        traceback.print_stack(frame, file=file)
        if thread is threading.main_thread():
            stack = traceback.extract_stack(frame)
            if stack:
                last = stack[-1]
                main_summary = (
                    f"{Path(last.filename).name}:{last.lineno} in {last.name}"
                )
    file.flush()
    return main_summary


@dataclass
class StageTimer:
    """Collect per-stage wall-clock durations for pipeline diagnostics."""

    stages: list[tuple[str, float]] = field(default_factory=list)
    _current_stage: str | None = field(default=None, init=False, repr=False)
    _current_started: float = field(default=0.0, init=False, repr=False)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        previous = self._current_stage
        self._begin(name)
        try:
            yield
        finally:
            self._end(name)
            self._current_stage = previous

    @property
    def current_stage(self) -> str | None:
        return self._current_stage

    def current_stage_elapsed(self) -> float:
        if self._current_stage is None:
            return 0.0
        return time.perf_counter() - self._current_started

    def as_dict(self) -> dict[str, float]:
        return {name: round(seconds, 3) for name, seconds in self.stages}

    def print_summary(self, *, header: str = "Pipeline stage timings") -> None:
        if not self.stages:
            return
        print(header)
        for name, seconds in self.stages:
            print(f"  {name}: {seconds:.1f}s")
        total = sum(seconds for _, seconds in self.stages)
        print(f"  total: {total:.1f}s")

    def record(self, name: str, seconds: float) -> None:
        """Record a sub-stage the caller timed itself, without printing."""
        self.stages.append((name, seconds))

    def _begin(self, name: str) -> None:
        self._current_stage = name
        self._current_started = time.perf_counter()

    def _end(self, name: str) -> None:
        elapsed = time.perf_counter() - self._current_started
        self.stages.append((name, elapsed))
        self._current_stage = None


@dataclass
class StallWatchdog:
    """Log the active stage and dump Python stacks if a stage runs too long."""

    timer: StageTimer
    interval_seconds: float
    log_path: Path | None = None
    _stop: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _log_handle: IO[str] | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.log_path.open("a", encoding="utf-8")
            faulthandler.enable(file=self._log_handle, all_threads=True)
        else:
            faulthandler.enable(all_threads=True)
        faulthandler.dump_traceback_later(
            self.interval_seconds,
            repeat=True,
            file=self._log_handle or sys.stderr,
        )
        self._thread = threading.Thread(
            target=self._run,
            name="pipeline-stall-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        faulthandler.cancel_dump_traceback_later()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._log_handle is not None:
            faulthandler.disable()
            self._log_handle.close()
            self._log_handle = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            stage = self.timer.current_stage
            if stage is None:
                continue
            elapsed = self.timer.current_stage_elapsed()
            message = (
                f"[pipeline] stage {stage!r} still running after {elapsed:.0f}s "
                f"(stall interval {self.interval_seconds:.0f}s)"
            )
            print(message, flush=True)
            target = self._log_handle or sys.stderr
            summary = dump_thread_stacks(
                target,
                header=f"{stage} after {elapsed:.0f}s",
            )
            if summary is not None:
                print(f"[pipeline] main thread blocked in {summary}", flush=True)
            if self._log_handle is not None:
                self._log_handle.write(message + "\n")
                self._log_handle.flush()


@contextmanager
def profile_if_enabled(
    output_dir: Path,
    *,
    enabled: bool | None = None,
    basename: str = "extraction",
) -> Iterator[cProfile.Profile | None]:
    """Optionally wrap work in cProfile and write binary + text summaries."""
    if enabled is None:
        enabled = env_flag("PIPELINE_PROFILE")
    if not enabled:
        yield None
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    prof_path = output_dir / f"{basename}.prof"
    text_path = output_dir / f"{basename}.pstats.txt"
    profile = cProfile.Profile()
    profile.enable()
    try:
        yield profile
    finally:
        profile.disable()
        profile.dump_stats(prof_path)
        stream = StringIO()
        stats = pstats.Stats(profile, stream=stream)
        stats.sort_stats("cumulative")
        stats.print_stats(40)
        text_path.write_text(stream.getvalue(), encoding="utf-8")
        print(f"Wrote profile {prof_path} and {text_path}")


@contextmanager
def monitor_pipeline_stage(
    timer: StageTimer,
    stage: str,
    *,
    stall_interval_seconds: float | None = None,
    stall_log_path: Path | None = None,
) -> Iterator[None]:
    """Time one stage and optionally attach a stall watchdog for that stage."""
    interval = (
        stall_interval_seconds
        if stall_interval_seconds is not None
        else env_float("PIPELINE_STALL_SECONDS")
    )
    watchdog: StallWatchdog | None = None
    with timer.stage(stage):
        if interval is not None and interval > 0:
            watchdog = StallWatchdog(
                timer=timer,
                interval_seconds=interval,
                log_path=stall_log_path,
            )
            watchdog.start()
        try:
            yield
        finally:
            if watchdog is not None:
                watchdog.stop()


def default_stall_log_path(output_dir: Path) -> Path:
    return output_dir / "extraction-stall.log"


def resolve_stall_log_path(output_dir: Path) -> Path:
    override = os.environ.get("PIPELINE_STALL_LOG")
    if override:
        return Path(override)
    return default_stall_log_path(output_dir)
