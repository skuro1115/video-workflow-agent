"""Per-stage timing collector for the pipeline.

Wrap each stage in ``with timer.stage("probe"):`` and the timer records its
elapsed time. ``to_dict()`` produces a stable JSON shape that ``main.py``
writes to ``output/run_timing.json``.

Used to make detector / encode chunk timing visible across runs, so future
tuning passes can compare e.g. whether a new RMS bin size made detection
slower without staring at wall clocks.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class StageTiming:
    name: str
    elapsed_seconds: float
    extra: dict = field(default_factory=dict)


class RunTimer:
    def __init__(self) -> None:
        self.stages: list[StageTiming] = []

    @contextmanager
    def stage(self, name: str, **extra) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages.append(StageTiming(
                name=name,
                elapsed_seconds=time.perf_counter() - t0,
                extra=extra,
            ))

    def to_dict(self) -> dict:
        return {
            "stages": [
                {"name": s.name, "elapsed_seconds": round(s.elapsed_seconds, 4), **s.extra}
                for s in self.stages
            ],
            "total_seconds": round(sum(s.elapsed_seconds for s in self.stages), 4),
        }
