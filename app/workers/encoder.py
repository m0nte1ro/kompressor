"""Encoder boundary. Future FFmpegEncoder owns all command/process knowledge."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models.queue import QueueJob


@dataclass(frozen=True)
class EncodeResult:
    job_id: str
    simulated: bool


@runtime_checkable
class Encoder(Protocol):
    def encode(self, job: QueueJob, progress_callback: Callable[[float], None]) -> EncodeResult: ...

    def stop(self, job_id: str) -> None: ...


class FakeEncoder:
    """Instant simulation; the fake worker supplies elapsed-time pacing.

    No files, measured sizes, or validation claims are fabricated. A production
    worker must run the blocking encoder outside HTTP and scheduler transactions.
    """
    def encode(self, job: QueueJob, progress_callback: Callable[[float], None]) -> EncodeResult:
        progress_callback(100.0)
        return EncodeResult(job_id=job.id, simulated=True)

    def stop(self, job_id: str) -> None:
        # Instant, stateless fake: no process or pending output to cancel.
        pass
