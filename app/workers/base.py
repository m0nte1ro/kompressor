from typing import Protocol

from app.models.queue import QueueJob


class EncoderWorker(Protocol):
    def advance(self, job: QueueJob, seconds: float) -> None: ...
