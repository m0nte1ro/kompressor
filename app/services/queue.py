"""Dual-lane scheduler. Policy decisions remain in the existing PolicyEngine."""
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from app.services.errors import Conflict, InvalidOperation, NotFound
from app.models.queue import EnqueueRequest, Priority, QueueJob
from app.models.preferences import WorkerSettings
from app.repositories.base import QueueRepository
from app.services.catalog import CatalogService
from app.services.worker_control import WorkerControlService
from app.workers.base import EncoderWorker


ACTIVE = {"encoding", "validating", "stopping"}
PENDING = {"queued", *ACTIVE}
PRIORITIES = {"urgent": 3, "high": 2, "normal": 1, "low": 0}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueConflict(Conflict):
    pass


class QueueService:
    def __init__(self, repository: QueueRepository, catalog: CatalogService,
                 worker: EncoderWorker, controls: WorkerControlService | None = None):
        self.repository = repository
        self.catalog = catalog
        self.worker = worker
        self.controls = controls
        self.execution_mode = getattr(worker, "execution_mode", "fake")
        self.external_backends = frozenset(getattr(worker, "external_backends", ()))
        self.supported_backends = frozenset(getattr(worker, "supported_backends", ("cpu", "qsv")))
        self.lock = RLock()
        self.move_sequence = max((j.move_next_order for j in repository.get_all()), default=0)

    @staticmethod
    def _payload(job: QueueJob) -> dict:
        payload = job.model_dump()
        if (job.execution_mode == "real" and job.status == "completed"
                and job.output_size is not None):
            # Older real jobs clamped negative savings to zero. Source/output
            # sizes are authoritative, so expose the measured delta correctly.
            payload["measured_saving"] = job.source_size - job.output_size
        return payload

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
                    "available": backend in self.supported_backends,
                    "active": self._payload(active) if active else None,
                    "queued": [self._payload(j) for j in self._queued(backend)],
                })
            return {
                "lanes": lanes,
                "history": [self._payload(j) for j in reversed(jobs) if j.status not in PENDING],
                "pending_count": sum(j.status in PENDING for j in jobs),
                "workers": self.controls.snapshot() if self.controls else None,
            }

    def _candidate_job(self, entry, preset, result, *, job_id: str,
                       requested_preserve_audio: bool | None,
                       preserve_subtitles: bool, replace_source: bool) -> QueueJob:
        return QueueJob(
            id=job_id, media_id=entry.item.id, scope=entry.scope,
            name=entry.name, backend=preset.backend, preset=preset.model_copy(deep=True),
            preserve_audio=result.preserve_audio,
            requested_preserve_audio=requested_preserve_audio,
            preserve_subtitles=preserve_subtitles,
            source_size=result.source_size, source_codec=entry.item.video_codec,
            source_file_id=entry.item.id if entry.item.revision_id else None,
            source_revision_id=entry.item.revision_id,
            source_reference=(
                getattr(self.worker, "capture_source_reference", lambda item: None)(entry.item)
            ),
            replace_source=replace_source,
            execution_mode=self.execution_mode,
            estimate_basis=result.estimate_basis,
            estimated_saving_low=result.estimated_saving_low,
            estimated_saving_high=result.estimated_saving_high,
            estimated_output_size=result.estimated_output_size,
            estimated_saving=result.estimated_saving,
            created_at=now(),
            planning_output_size=result.planning_output_size,
            planning_saving=result.planning_saving,
            planning_saving_percent=result.planning_saving_percent,
        )

    def execution_reasons(self, entry, preset, result, *,
                          requested_preserve_audio: bool | None,
                          preserve_subtitles: bool, replace_source: bool = False) -> list[str]:
        reasons = []
        if preset.backend not in self.supported_backends:
            reasons.append(f"{preset.backend.upper()} real encoding is not available in this runtime.")
        if preset.destination_codec != "hevc":
            reasons.append("AV1 is not available in this workflow.")
        if result.reasons or reasons:
            return reasons
        job = self._candidate_job(
            entry, preset, result, job_id="eligibility-preview",
            requested_preserve_audio=requested_preserve_audio,
            preserve_subtitles=preserve_subtitles,
            replace_source=replace_source,
        )
        capability_check = getattr(self.worker, "enqueue_reasons", None)
        if capability_check:
            reasons.extend(capability_check(entry.item, job))
        return list(dict.fromkeys(reasons))

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
                    reasons.append(f"{preset.backend.upper()} real encoding is not available in this runtime.")
                if any(j.media_id == media_id and j.scope == request.scope and j.status in PENDING
                       for j in self.repository.get_all()):
                    reasons.append("Already queued or active.")
                if preset.destination_codec != "hevc":
                    reasons.append("AV1 is not available in this workflow.")
                if reasons:
                    excluded.append({"media_id": media_id, "reasons": reasons})
                    continue
                job = self._candidate_job(
                    entry, preset, result, job_id=str(uuid4()),
                    requested_preserve_audio=request.preserve_audio,
                    preserve_subtitles=result.preserve_subtitles,
                    replace_source=request.replace_source,
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

    @staticmethod
    def _request_cancel(job: QueueJob, reason: str) -> None:
        job.status = "stopping"
        job.cancel_requested = True
        job.cancel_reason = reason

    def skip(self, job_id: str, reason: str = "user_stop") -> None:
        if self._find_external_active(job_id):
            # The HTTP process does not own the ffmpeg subprocess. Persist the
            # cancellation request; the standalone worker observes it and stops
            # only its own process.
            with self.lock, self.repository.transaction():
                job = self._find(job_id)
                if job.status == "completed":
                    raise QueueConflict("The job completed before Stop & Skip took effect.")
                if job.status not in ACTIVE:
                    raise QueueConflict("Only active jobs can be stopped and skipped.")
                if job.status != "stopping":
                    self._request_cancel(job, reason)
                    self.repository.save(job)
            return
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status not in ACTIVE:
                raise QueueConflict("Only active jobs can be stopped and skipped.")
            self.worker.stop(job.id)
            job.status = "skipped"
            job.finished_at = now()
            job.cancel_requested = False
            job.cancel_reason = reason
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
            if self.controls and not self.controls.can_claim(backend):
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

    def recover(self, backends: set[str] | frozenset[str] | None = None) -> None:
        """Recover only lanes owned by this process; seed recovery still covers all lanes."""
        interrupted = []
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if backends is not None and job.backend not in backends:
                    continue
                if job.status == "stopping" and job.cancel_requested:
                    if job.backend in self.external_backends:
                        interrupted.append(job.id)
                    job.status = "skipped"
                    job.finished_at = now()
                    if job.cancel_reason == "quiet_hours_cutoff":
                        job.reasons = [*job.reasons, "Stopped at quiet-hours start at or below the configured progress cutoff."]
                    elif job.cancel_reason == "user_stop":
                        job.reasons = [*job.reasons, "Stopped by user."]
                    job.output_path = None
                    job.output_size = None
                    job.measured_saving = None
                    job.error_message = None
                    job.ffmpeg_exit_code = None
                    job.validation_errors = []
                    job.cancel_requested = False
                    self.repository.save(job)
                elif job.status in ACTIVE:
                    if job.execution_mode != self.execution_mode:
                        job.status = "blocked"
                        job.reasons = [f"This {job.execution_mode} job cannot resume in {self.execution_mode} mode."]
                        job.finished_at = now()
                    else:
                        # Runtime lane availability is transient (for example a
                        # render device can disappear across a host/LXC restart).
                        # An owned interrupted real job is requeued and waits for
                        # its worker instead of becoming permanently blocked.
                        if job.backend in self.external_backends:
                            interrupted.append(job.id)
                        job.status = "queued"
                        job.progress = 0
                        job.progress_known = False
                        job.elapsed_seconds = 0
                        job.started_at = None
                        job.cancel_requested = False
                        job.cancel_reason = None
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
                elif job.status == "blocked" and job.cancel_requested:
                    # A prior worker may have died after policy revalidation made
                    # the job terminal but before acknowledging the stop request.
                    job.cancel_requested = False
                    self.repository.save(job)
            if backends is None:
                self.revalidate()
            else:
                ready_backends = frozenset(backends) & self.supported_backends
                if ready_backends:
                    self.revalidate(backends=ready_backends)
        cleanup = getattr(self.worker, "cleanup_interrupted", None)
        if cleanup:
            for job_id in interrupted:
                cleanup(job_id)

    def claim_next(self, backend: str) -> QueueJob | None:
        with self.lock, self.repository.transaction():
            if backend not in self.external_backends or backend not in self.supported_backends:
                return None
            if self.controls and not self.controls.can_claim(backend):
                return None
            if any(j.backend == backend and j.status in ACTIVE for j in self.repository.get_all()):
                return None
            self.revalidate(backends={backend})
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
                job.progress_known = True
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

    def complete_real_job(self, job_id: str, output_path: str, output_size: int) -> bool:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "validating" or job.cancel_requested:
                return False
            job.status = "completed"
            job.progress = 100
            job.finished_at = now()
            job.output_path = output_path
            job.output_size = output_size
            job.measured_saving = job.source_size - output_size
            job.error_message = None
            job.cancel_requested = False
            job.cancel_reason = None
            self.repository.save(job)
            return True

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
            # Service shutdown deliberately leaves ordinary encoding/validating
            # state for conservative restart recovery. Persisted cancellation
            # requests, however, become explicit skipped history only after the
            # owning worker has stopped its ffmpeg process.
            if job.status == "blocked" and job.cancel_reason == "policy_changed":
                job.cancel_requested = False
                self.repository.save(job)
                return
            if job.cancel_requested or job.status == "stopping":
                reason = job.cancel_reason
                job.status = "skipped"
                job.finished_at = now()
                job.output_path = None
                job.output_size = None
                job.measured_saving = None
                job.error_message = None
                job.ffmpeg_exit_code = None
                job.validation_errors = []
                if reason == "quiet_hours_cutoff":
                    job.reasons = [*job.reasons, "Stopped at quiet-hours start at or below the configured progress cutoff."]
                elif reason == "user_stop":
                    job.reasons = [*job.reasons, "Stopped by user."]
                job.cancel_requested = False
                self.repository.save(job)

    def cancel_requested(self, job_id: str) -> bool:
        with self.lock, self.repository.transaction():
            return self._find(job_id).cancel_requested

    def quiet_active(self, backend: str) -> bool:
        return bool(self.controls and self.controls.quiet_active(backend))

    def enforce_quiet_start(self, backend: str) -> str | None:
        if self.controls is None or not self.controls.quiet_active(backend):
            return None
        with self.lock, self.repository.transaction():
            active = next((j for j in self.repository.get_all()
                           if j.backend == backend and j.status in ACTIVE), None)
            if active is None:
                return None
            action = self.controls.quiet_action(backend, active)
            if action == "stop" and active.backend in self.external_backends:
                self._request_cancel(active, "quiet_hours_cutoff")
                self.repository.save(active)
            return action

    def get_worker_settings(self) -> dict:
        return self.controls.snapshot() if self.controls else {
            "timezone": "UTC", "lanes": {
                backend: {"paused": False, "quiet_hours_enabled": False,
                          "quiet_start": "18:00", "quiet_end": "01:00",
                          "quiet_cutoff_percent": 50.0, "quiet_active": False,
                          "accepting_jobs": True}
                for backend in ("cpu", "qsv")
            }}

    def update_worker_settings(self, settings: WorkerSettings) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        with self.lock:
            current = self.controls.get()
            settings = settings.model_copy(update={
                "cpu": settings.cpu.model_copy(update={"paused": current.cpu.paused}),
                "qsv": settings.qsv.model_copy(update={"paused": current.qsv.paused}),
            })
            self.controls.save(settings)
            snapshot = self.controls.snapshot()
        wake = getattr(self.worker, "wake", None)
        if wake:
            wake()
        return snapshot

    def set_worker_paused(self, backend: str, paused: bool) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        if backend not in {"cpu", "qsv"}:
            raise InvalidOperation("Unknown worker backend.")
        with self.lock:
            self.controls.set_paused(backend, paused)
            snapshot = self.controls.snapshot()
        wake = getattr(self.worker, "wake", None)
        if wake:
            wake()
        return snapshot

    def pause_all_workers(self, *, stop_active: bool = False) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        with self.lock:
            self.controls.pause_all()
            if stop_active:
                for backend in ("cpu", "qsv"):
                    self.stop_active_backend(backend, reason="user_stop")
            snapshot = self.controls.snapshot()
        return snapshot

    def resume_all_workers(self) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        with self.lock:
            self.controls.resume_all()
            snapshot = self.controls.snapshot()
        wake = getattr(self.worker, "wake", None)
        if wake:
            wake()
        return snapshot

    def stop_active_backend(self, backend: str, reason: str = "user_stop") -> bool:
        with self.lock, self.repository.transaction():
            active = next((j for j in self.repository.get_all()
                           if j.backend == backend and j.status in ACTIVE), None)
            job_id = active.id if active else None
        if job_id is None:
            return False
        self.skip(job_id, reason=reason)
        return True

    def revalidate(self, *, defer_external_stops: bool = False,
                   backends: set[str] | frozenset[str] | None = None) -> list[str]:
        """Apply current protections, optionally restricted to process-owned lanes."""
        stop_after_commit = []
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if backends is not None and job.backend not in backends:
                    continue
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
                    if job.execution_mode == "real":
                        # Runtime availability is checked when new work is
                        # submitted/claimed. Do not turn durable queued work
                        # terminal merely because binaries, workspace or a
                        # render device are temporarily unavailable.
                        reasons = [
                            reason for reason in reasons
                            if reason != "Read-only filesystem inventory: encoding is not enabled."
                        ]
                    if job.execution_mode != self.execution_mode:
                        reasons.append(f"This {job.execution_mode} job cannot run in {self.execution_mode} mode.")
                    # Runtime lane availability is not a terminal policy.
                    # Existing jobs remain queued while a CPU/QSV worker is
                    # temporarily unavailable and resume when that lane returns.
                    if job.status in ACTIVE and result.preserve_audio != job.preserve_audio:
                        reasons.append("Audio policy changed during execution. Submit a new job.")
                    capability_check = getattr(self.worker, "enqueue_reasons", None)
                    if capability_check and job.status == "queued":
                        capability_reasons = capability_check(entry.item, job)
                        if job.execution_mode == "real":
                            capability_reasons = [
                                reason for reason in capability_reasons
                                if reason != "Real encoding prerequisites are unavailable."
                            ]
                        reasons.extend(capability_reasons)
                except LookupError as error:
                    reasons = [str(error)]
                if reasons:
                    if job.status in ACTIVE:
                        if job.backend in self.external_backends:
                            job.cancel_requested = True
                            job.cancel_reason = "policy_changed"
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
        # External workers observe persisted cancellation requests in
        # SQLite. Never call a process-local encoder handle from the web process.
        if defer_external_stops:
            return stop_after_commit
        return []
