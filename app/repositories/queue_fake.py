"""Process-local fake state, seeded once. Never writes fixtures or media files."""
import json
from contextlib import nullcontext
from pathlib import Path

from app.models.queue import QueueJob


class FakeQueueRepository:
    def __init__(self, fixture_path: Path):
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.jobs = [QueueJob.model_validate(job) for job in payload["jobs"]]

    def get_all(self) -> list[QueueJob]:
        return self.jobs

    def add(self, job: QueueJob) -> None:
        self.jobs.append(job)

    def remove(self, job_id: str) -> None:
        self.jobs = [job for job in self.jobs if job.id != job_id]

    def save(self, job: QueueJob) -> None:
        self.jobs = [job if current.id == job.id else current for current in self.jobs]

    def transaction(self):
        return nullcontext()
