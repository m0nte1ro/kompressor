"""Dual-lane scheduler. Policy decisions remain in the existing PolicyEngine."""
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from app.services.errors import Conflict, InvalidOperation, NotFound
from app.models.queue import EnqueueRequest, Priority, QueueJob
from app.repositories.base import QueueRepository
from app.services.catalog import CatalogService
from app.workers.base import EncoderWorker


ACTIVE = {"encoding", "validating"}
PENDING = {"queued", *ACTIVE}
PRIORITIES = {"urgent": 3, "high": 2, "normal": 1, "low": 0}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueConflict(Conflict):
    pass


class QueueService:
    def __init__(self, repository: QueueRepository, catalog: CatalogService,
                 worker: EncoderWorker):
        self.repository = repository
        self.catalog = catalog
        self.worker = worker
        self.execution_mode = getattr(worker, "execution_mode", "fake")
        self.external_backends = frozenset(getattr(worker, "external_backends", ()))
        self.supported_backends = frozenset(getattr(worker, "supported_backends", ("cpu", "qsv")))
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
        if getattr(self.worker, "filesystem_mode", False) and not getattr(self.worker, "enabled", False):
            raise InvalidOperation("Real encoding is unavailable: " + (getattr(self.worker, "unavailable_reason", None) or "runtime prerequisites failed."))
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
                if preset.backend not in self.supported_backends:
                    reasons.append(f"{preset.backend.upper()} real encoding is not enabled.")
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
                    source_file_id=entry.item.id if entry.item.revision_id else None,
                    source_revision_id=entry.item.revision_id,
                    source_reference=(getattr(self.worker, "capture_source_reference", lambda item: None)(entry.item)),
                    replace_source=request.replace_source,
                    execution_mode=self.execution_mode,
                    estimate_basis=result.estimate_basis,
                    estimated_saving_low=result.estimated_saving_low,
                    estimated_saving_high=result.estimated_saving_high,
                    estimated_output_size=result.estimated_output_size,
                    estimated_saving=result.estimated_saving, created_at=now(),
                    planning_output_size=result.planning_output_size,
                    planning_saving=result.planning_saving,
                    planning_saving_percent=result.planning_saving_percent,
                )
                capability_check = getattr(self.worker, "enqueue_reasons", None)
                if capability_check:
                    reasons.extend(capability_check(entry.item, job))
                if reasons:
                    excluded.append({"media_id": media_id, "reasons": list(dict.fromkeys(reasons))})
                    continue
                self.repository.add(job)
                added.append(job.model_dump())
        if added:
            wake = getattr(self.worker, "wake", None)
            if wake:
                wake()
        return {"added": added, "excluded": excluded}

    def _find(self, job_id: str) -> QueueJob:
        job = next((j for j in self.repository.get_all() if j.id == job_id), None)
        if job is None:
            raise NotFound("Job not found.")
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
        if self._find_external_active(job_id):
            # Never wait for ffmpeg while holding the queue lock or a DB transaction.
            # Mark cancellation, terminate/kill the owned process, and clean its
            # partial before persisting the user-visible skipped state.
            self.worker.stop(job_id)
            with self.lock, self.repository.transaction():
                job = self._find(job_id)
                if job.status == "completed":
                    raise QueueConflict("The job completed before Stop & Skip took effect.")
                if job.status not in ACTIVE and job.status != "failed":
                    raise QueueConflict("Only active jobs can be stopped and skipped.")
                job.status = "skipped"
                job.finished_at = now()
                job.error_message = None
                job.ffmpeg_exit_code = None
                job.output_path = None
                job.output_size = None
                job.measured_saving = None
                job.validation_errors = []
                self.repository.save(job)
            wake = getattr(self.worker, "wake", None)
            if wake:
                wake()
            return
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status not in ACTIVE:
                raise QueueConflict("Only active jobs can be stopped and skipped.")
            self.worker.stop(job.id)
            job.status = "skipped"
            job.finished_at = now()
            self.repository.save(job)
            self.revalidate()
            self._start_idle_lanes()

    def _find_external_active(self, job_id: str) -> bool:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            return job.backend in self.external_backends and job.status in ACTIVE

    def _start_idle_lanes(self) -> None:
        for backend in ("cpu", "qsv"):
            if backend in self.external_backends:
                continue
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
                if job.backend in self.external_backends:
                    continue
                if job.status in ACTIVE:
                    self.worker.advance(job, seconds)
                    if job.status == "completed":
                        job.finished_at = now()
                    self.repository.save(job)
            self._start_idle_lanes()

    def recover(self) -> None:
        """Requeue interrupted real jobs from zero and remove unvalidated workspace outputs."""
        interrupted = []
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if job.status in ACTIVE:
                    if job.execution_mode != self.execution_mode:
                        job.status = "blocked"
                        job.reasons = [f"This {job.execution_mode} job cannot resume in {self.execution_mode} mode."]
                        job.finished_at = now()
                    elif job.backend not in self.supported_backends:
                        job.status = "blocked"
                        job.reasons = [f"{job.backend.upper()} worker is not available in this runtime."]
                        job.finished_at = now()
                    else:
                        if job.backend in self.external_backends:
                            interrupted.append(job.id)
                        job.status = "queued"
                        job.progress = 0
                        job.elapsed_seconds = 0
                        job.started_at = None
                    job.output_path = None
                    job.output_size = None
                    job.measured_saving = None
                    job.error_message = None
                    job.ffmpeg_exit_code = None
                    job.validation_errors = []
                    self.repository.save(job)
                elif job.status == "queued" and job.execution_mode != self.execution_mode:
                    job.status = "blocked"
                    job.reasons = [f"This {job.execution_mode} job cannot run in {self.execution_mode} mode."]
                    job.finished_at = now()
                    self.repository.save(job)
                elif job.status == "queued" and job.backend not in self.supported_backends:
                    job.status = "blocked"
                    job.reasons = [f"{job.backend.upper()} worker is not available in this runtime."]
                    job.finished_at = now()
                    self.repository.save(job)
            self.revalidate()
        cleanup = getattr(self.worker, "cleanup_interrupted", None)
        if cleanup:
            for job_id in interrupted:
                cleanup(job_id)

    def claim_next(self, backend: str) -> QueueJob | None:
        with self.lock, self.repository.transaction():
            if backend not in self.external_backends or backend not in self.supported_backends:
                return None
            if any(j.backend == backend and j.status in ACTIVE for j in self.repository.get_all()):
                return None
            self.revalidate()
            queued = self._queued(backend)
            if not queued:
                return None
            job = queued[0]
            job.status = "encoding"
            job.started_at = now()
            self.repository.save(job)
            return job.model_copy(deep=True)

    def update_real_progress(self, job_id: str, percent: float | None, elapsed: float) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "encoding":
                return
            if percent is not None:
                job.progress = max(job.progress, min(99.0, percent))
            job.elapsed_seconds = max(job.elapsed_seconds, elapsed)
            self.repository.save(job)

    def set_real_validating(self, job_id: str, output_path: str) -> bool:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "encoding":
                return False
            job.status = "validating"
            job.progress = 100
            job.output_path = output_path
            self.repository.save(job)
            return True

    def record_validation_errors(self, job_id: str, errors: list[str]) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status == "validating":
                job.validation_errors = errors
                self.repository.save(job)

    def complete_real_job(self, job_id: str, output_path: str, output_size: int) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "validating":
                return
            job.status = "completed"
            job.progress = 100
            job.finished_at = now()
            job.output_path = output_path
            job.output_size = output_size
            job.measured_saving = max(0, job.source_size - output_size)
            job.error_message = None
            self.repository.save(job)

    def fail_real_job(self, job_id: str, message: str, exit_code: int | None = None) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status not in ACTIVE:
                return
            job.status = "failed"
            job.finished_at = now()
            job.error_message = message
            job.ffmpeg_exit_code = exit_code
            job.output_path = None
            job.output_size = None
            job.measured_saving = None
            self.repository.save(job)

    def cancelled_real_job(self, job_id: str) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            # QueueService persists Stop & Skip after process termination. Shutdown
            # deliberately leaves active state for conservative restart recovery.
            if job.status == "skipped":
                job.finished_at = job.finished_at or now()
                self.repository.save(job)

    def revalidate(self, *, defer_external_stops: bool = False) -> list[str]:
        """Apply current protections; optionally let a caller stop real workers after its transaction."""
        stop_after_commit = []
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if job.status not in PENDING:
                    continue
                before = job.model_dump()
                result = None
                try:
                    entry = self.catalog.find(job.media_id, job.scope)
                    requested = job.requested_preserve_audio
                    result = self.catalog.evaluate(entry, job.preset,
                        job.preserve_audio if requested is None else requested, job.preserve_subtitles)
                    reasons = list(result.reasons)
                    if job.execution_mode != self.execution_mode:
                        reasons.append(f"This {job.execution_mode} job cannot run in {self.execution_mode} mode.")
                    if job.backend not in self.supported_backends:
                        reasons.append(f"{job.backend.upper()} real encoding is not enabled.")
                    if job.status in ACTIVE and result.preserve_audio != job.preserve_audio:
                        reasons.append("Audio policy changed during execution. Submit a new job.")
                    capability_check = getattr(self.worker, "enqueue_reasons", None)
                    if capability_check and job.status == "queued":
                        reasons.extend(capability_check(entry.item, job))
                except LookupError as error:
                    reasons = [str(error)]
                if reasons:
                    if job.status in ACTIVE:
                        if job.backend in self.external_backends:
                            stop_after_commit.append(job.id)
                        else:
                            self.worker.stop(job.id)
                    job.status = "blocked"
                    job.reasons = reasons
                    job.finished_at = now()
                elif job.status == "queued":
                    assert result is not None
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
        if defer_external_stops:
            return stop_after_commit
        for job_id in stop_after_commit:
            self.worker.stop(job_id)
        return []
