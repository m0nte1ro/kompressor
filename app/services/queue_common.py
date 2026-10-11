"""Pieces every part of the queue service shares."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Literal

from app.models.queue import ACTIVE_STATUSES, PENDING_STATUSES, QueueJob
from app.repositories.base import QueueRepository
from app.services.catalog import CatalogService
from app.services.errors import Conflict, NotFound
from app.services.worker_control import WorkerControlService
from app.workers.base import EncoderWorker


ACTIVE = ACTIVE_STATUSES
PENDING = PENDING_STATUSES


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueConflict(Conflict):
    pass


@dataclass(frozen=True)
class StorageChecks:
    """What library storage said, gathered before a transaction opened.

    Listing a root or stat'ing a source blocks while its network mount hangs,
    and SQLite's write lock must never wait on library storage.
    """
    unavailable_roots: frozenset[str] = frozenset()
    # Replacement preconditions (path, writable mount) of queued replace jobs, by job ID.
    replacement: Mapping[str, list[str]] = field(default_factory=dict)


class QueueParts:
    """What the queue service's parts share. QueueService sets it up and combines
    the parts; they call each other's methods through self."""
    repository: QueueRepository
    catalog: CatalogService
    worker: EncoderWorker
    controls: WorkerControlService | None
    lock: RLock
    execution_mode: Literal["fake", "real"]
    external_backends: frozenset[str]
    supported_backends: frozenset[str]

    def _find(self, job_id: str) -> QueueJob:
        job = self.repository.get(job_id)
        if job is None:
            raise NotFound("Job not found.")
        return job

    @staticmethod
    def _request_cancel(job: QueueJob, reason: str) -> None:
        job.move_to("stopping")
        job.cancel_requested = True
        job.cancel_reason = reason
