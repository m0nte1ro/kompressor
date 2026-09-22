"""Dual-lane scheduler. Policy decisions remain in the existing PolicyEngine."""
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from app.models.queue import EnqueueRequest, Priority, QueueJob
from app.repositories.base import QueueRepository
from app.services.catalog import CatalogService
from app.workers.base import EncoderWorker


ACTIVE = {"encoding", "validating"}
PENDING = {"queued", *ACTIVE}
PRIORITIES = {"urgent": 3, "high": 2, "normal": 1, "low": 0}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueConflict(ValueError):
    pass


class QueueService:
    def __init__(self, repository: QueueRepository, catalog: CatalogService,
                 worker: EncoderWorker):
        self.repository = repository
        self.catalog = catalog
        self.worker = worker
        self.lock = RLock()
        self.move_sequence = max((j.move_next_order for j in repository.get_all()), default=0)

    def _queued(self, backend: str) -> list[QueueJob]:
        return sorted(
            (j for j in self.repository.get_all() if j.backend == backend and j.status == "queued"),
            key=lambda j: (-j.move_next_order, -PRIORITIES[j.priority],
                           -(j.estimated_saving if j.estimated_saving is not None else j.planning_saving or 0),
                           j.created_at, j.id),
        )

    def snapshot(self) -> dict:
        with self.lock, self.repository.transaction():
            jobs = self.repository.get_all()
            lanes = []
            for backend in ("cpu", "qsv"):
                active = next((j for j in jobs if j.backend == backend and j.status in ACTIVE), None)
                lanes.append({
                    "backend": backend,
                    "active": active.model_dump() if active else None,
                    "queued": [j.model_dump() for j in self._queued(backend)],
                })
            return {
                "lanes": lanes,
                "history": [j.model_dump() for j in reversed(jobs) if j.status not in PENDING],
                "pending_count": sum(j.status in PENDING for j in jobs),
            }

    def enqueue(self, request: EnqueueRequest) -> dict:
        # Validate the preset before any mutation. Scope mismatches are also
        # evaluated per item by the policy engine and returned as exclusions.
        preset = self.catalog.preset(request.preset_id)
        added, excluded = [], []
        with self.lock, self.repository.transaction():
            for media_id in dict.fromkeys(request.media_ids):
                try:
                    entry = self.catalog.find(media_id, request.scope)
                except LookupError as error:
                    excluded.append({"media_id": media_id, "reasons": [str(error)]})
                    continue
                result = self.catalog.evaluate(entry, preset, request.preserve_audio,
                                               request.preserve_subtitles)
                reasons = list(result.reasons)
                if any(j.media_id == media_id and j.scope == request.scope and j.status in PENDING
                       for j in self.repository.get_all()):
                    reasons.append("Already queued or active.")
                if preset.destination_codec != "hevc":
                    reasons.append("AV1 is not available in this workflow.")
                if reasons:
                    excluded.append({"media_id": media_id, "reasons": reasons})
                    continue
                job = QueueJob(
                    id=str(uuid4()), media_id=media_id, scope=request.scope,
                    name=entry.name, backend=preset.backend, preset=preset.model_copy(deep=True),
                    preserve_audio=result.preserve_audio,
                    requested_preserve_audio=request.preserve_audio,
                    preserve_subtitles=result.preserve_subtitles,
                    source_size=result.source_size, source_codec=entry.item.video_codec,
                    replace_source=request.replace_source, estimate_basis=result.estimate_basis,
                    estimated_saving_low=result.estimated_saving_low,
                    estimated_saving_high=result.estimated_saving_high,
                    estimated_output_size=result.estimated_output_size,
                    estimated_saving=result.estimated_saving, created_at=now(),
                    planning_output_size=result.planning_output_size,
                    planning_saving=result.planning_saving,
                    planning_saving_percent=result.planning_saving_percent,
                )
                self.repository.add(job)
                added.append(job.model_dump())
        return {"added": added, "excluded": excluded}

    def _find(self, job_id: str) -> QueueJob:
        job = next((j for j in self.repository.get_all() if j.id == job_id), None)
        if job is None:
            raise LookupError("Job not found.")
        return job

    def remove(self, job_id: str) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "queued":
                raise QueueConflict("Only queued jobs can be removed. Use Stop & Skip for active jobs.")
            self.repository.remove(job_id)

    def prioritize(self, job_id: str, priority: Priority | None = None) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "queued":
                raise QueueConflict("Only queued jobs can be reordered.")
            if priority is None:
                self.move_sequence += 1
                job.move_next_order = self.move_sequence
            else:
                job.priority = priority
                job.move_next_order = 0
            self.repository.save(job)

    def skip(self, job_id: str) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status not in ACTIVE:
                raise QueueConflict("Only active jobs can be stopped and skipped.")
            job.status = "skipped"
            job.finished_at = now()
            self.repository.save(job)
            self.revalidate()
            self._start_idle_lanes()

    def _start_idle_lanes(self) -> None:
        for backend in ("cpu", "qsv"):
            if any(j.backend == backend and j.status in ACTIVE for j in self.repository.get_all()):
                continue
            queued = self._queued(backend)
            if queued:
                job = queued[0]
                job.status = "encoding"
                job.started_at = now()
                self.repository.save(job)

    def tick(self, seconds: float) -> None:
        """Called by the fake worker clock, never by GET requests."""
        with self.lock, self.repository.transaction():
            self.revalidate()
            for job in self.repository.get_all():
                if job.status in ACTIVE:
                    self.worker.advance(job, seconds)
                    if job.status == "completed":
                        job.finished_at = now()
                    self.repository.save(job)
            self._start_idle_lanes()

    def recover(self) -> None:
        """Restart interrupted simulations from zero; never count downtime as progress."""
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if job.status in ACTIVE:
                    job.status = "queued"
                    job.progress = 0
                    job.elapsed_seconds = 0
                    job.started_at = None
                    self.repository.save(job)
            self.revalidate()

    def revalidate(self) -> None:
        """Apply current media protections to preset snapshots before worker execution."""
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if job.status not in PENDING:
                    continue
                before = job.model_dump()
                try:
                    entry = self.catalog.find(job.media_id, job.scope)
                    requested = job.requested_preserve_audio
                    result = self.catalog.evaluate(entry, job.preset,
                        job.preserve_audio if requested is None else requested, job.preserve_subtitles)
                    reasons = list(result.reasons)
                    if job.status in ACTIVE and result.preserve_audio != job.preserve_audio:
                        reasons.append("Audio policy changed during simulation. Submit a new job.")
                except LookupError as error:
                    reasons = [str(error)]
                if reasons:
                    job.status = "blocked"
                    job.reasons = reasons
                    job.finished_at = now()
                elif job.status == "queued":
                    job.preserve_audio = result.preserve_audio
                    job.source_size = result.source_size
                    job.estimated_output_size = result.estimated_output_size
                    job.estimated_saving = result.estimated_saving
                    job.estimate_basis = result.estimate_basis
                    job.estimated_saving_low = result.estimated_saving_low
                    job.estimated_saving_high = result.estimated_saving_high
                    job.planning_output_size = result.planning_output_size
                    job.planning_saving = result.planning_saving
                    job.planning_saving_percent = result.planning_saving_percent
                if job.model_dump() != before:
                    self.repository.save(job)
