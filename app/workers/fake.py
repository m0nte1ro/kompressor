"""Deterministic elapsed-time simulation; no encoder, subprocess or media IO."""
from app.models.queue import QueueJob


class FakeEncoderWorker:
    encode_seconds = 180
    validation_seconds = 5

    def advance(self, job: QueueJob, seconds: float) -> None:
        job.elapsed_seconds += max(0, seconds)
        job.progress = min(100, round(job.elapsed_seconds / self.encode_seconds * 100, 1))
        if job.elapsed_seconds >= self.encode_seconds + self.validation_seconds:
            job.status = "completed"
        elif job.elapsed_seconds >= self.encode_seconds:
            job.status = "validating"
