"""Deterministic elapsed-time simulation; no subprocess or media IO."""
from app.models.queue import QueueJob
from app.workers.encoder import Encoder


class FakeEncoderWorker:
    encode_seconds = 180
    validation_seconds = 5

    def __init__(self, encoder: Encoder):
        self.encoder = encoder

    def advance(self, job: QueueJob, seconds: float) -> None:
        job.elapsed_seconds += max(0, seconds)
        job.progress = min(100, round(job.elapsed_seconds / self.encode_seconds * 100, 1))
        if job.elapsed_seconds >= self.encode_seconds:
            if job.status == "encoding":
                result = self.encoder.encode(job, lambda progress: setattr(job, "progress", progress))
                if not result.simulated:
                    raise RuntimeError("The fake worker only accepts simulated encoder results.")
            job.status = "validating"
        if job.elapsed_seconds >= self.encode_seconds + self.validation_seconds:
            job.status = "completed"

    def stop(self, job_id: str) -> None:
        self.encoder.stop(job_id)
