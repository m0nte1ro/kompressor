"""Dual-lane scheduler. Policy decisions remain in the existing PolicyEngine.

QueueService combines this scheduler with the queue's other parts: the real lanes'
job lifecycle (queue_lifecycle), startup recovery (queue_recovery), worker controls
and quiet hours (queue_controls) and History (queue_history). Every status change
goes through QueueJob.move_to and its JOB_TRANSITIONS table.
"""
import logging
from threading import RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from app.models.queue import EnqueueRequest, Priority, QueueJob
from app.repositories.base import QueueRepository
from app.services.catalog import CatalogService
from app.services.errors import InvalidOperation
from app.services.queue_common import ACTIVE, PENDING, QueueConflict, StorageChecks, now
from app.services.queue_controls import WorkerControls
from app.services.queue_history import QueueHistory
from app.services.queue_lifecycle import RealJobLifecycle
from app.services.queue_recovery import QueueRecovery
from app.services.worker_control import WorkerControlService
from app.workers.base import EncoderWorker

__all__ = ["ACTIVE", "PENDING", "QueueConflict", "QueueService", "StorageChecks", "now"]

PRIORITIES = {"urgent": 3, "high": 2, "normal": 1, "low": 0}
# How long a lane whose queued sources are all unreachable waits before looking again.
OUTAGE_BACKOFF_SECONDS = 10.0
# Shown on a queued job while its library root is down; removed once it is back.
WAITING_FOR_ROOT = "Waiting: the library root holding this source is unavailable (unmounted or unreachable)."

log = logging.getLogger(__name__)


class QueueService(QueueHistory, QueueRecovery, RealJobLifecycle, WorkerControls):
    """Enqueueing, ordering, claiming and revalidating jobs across the CPU and GPU lanes."""

    def __init__(self, repository: QueueRepository, catalog: CatalogService,
                 worker: EncoderWorker, controls: WorkerControlService | None = None):
        self.repository = repository
        self.catalog = catalog
        self.worker = worker
        self.controls = controls
        self.execution_mode: Literal["fake", "real"] = (
            "real" if getattr(worker, "filesystem_mode", False) else "fake"
        )
        self.external_backends = frozenset(getattr(worker, "external_backends", ()))
        self.supported_backends = frozenset(getattr(worker, "supported_backends", ("cpu", "gpu")))
        self.lock = RLock()
        self._claim_backoff: dict[str, float] = {}
        self._lanes_waiting: set[str] = set()
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

    def storage_checks(self, backends: set[str] | frozenset[str] | None = None) -> StorageChecks:
        """Check library storage for the pending real jobs: which roots are down, and
        whether each queued replace job's source can still be replaced.

        Runs before a transaction opens, never inside one. Callers inside a
        transaction hand the result to revalidate().
        """
        unavailable_roots = getattr(self.worker, "unavailable_roots", None)
        replacement_check = getattr(self.worker, "replacement_check", None)
        if unavailable_roots is None or replacement_check is None:
            return StorageChecks()
        jobs = [job for job in self.repository.pending() if job.execution_mode == "real"
                and job.source_reference is not None and (backends is None or job.backend in backends)]
        roots = {job.source_reference.root_id for job in jobs if job.source_reference.root_id is not None}
        unavailable = unavailable_roots(roots) if roots else frozenset()
        replacement = {job.id: replacement_check(job.source_reference) for job in jobs
                       if job.status == "queued" and job.replace_source
                       and not self._awaits_root(job, unavailable)}
        return StorageChecks(unavailable, replacement)

    @staticmethod
    def _awaits_root(job: QueueJob, unavailable: frozenset[str]) -> bool:
        """A queued real job whose library root is down waits instead of being judged."""
        reference = job.source_reference
        return (job.status == "queued" and job.execution_mode == "real" and reference is not None
                and reference.root_id in unavailable)

    def _queued(self, backend: str, jobs: list[QueueJob] | None = None) -> list[QueueJob]:
        source = self.repository.pending() if jobs is None else jobs
        return sorted(
            (j for j in source if j.backend == backend and j.status == "queued"),
            key=lambda j: (-j.move_next_order, -PRIORITIES[j.priority],
                           -(j.estimated_saving if j.estimated_saving is not None else j.planning_saving or 0),
                           j.created_at, j.id),
        )

    def snapshot(self) -> dict:
        # get_all() already uses a SQLite read snapshot. Do not reserve the global
        # writer lock for browser polling now that CPU and GPU are separate processes.
        with self.lock:
            jobs = self.repository.get_all()
            lanes = []
            for backend in ("cpu", "gpu"):
                active = next((j for j in jobs if j.backend == backend and j.status in ACTIVE), None)
                lanes.append({
                    "backend": backend,
                    "available": backend in self.supported_backends,
                    "active": self._payload(active) if active else None,
                    "queued": [self._payload(j) for j in self._queued(backend, jobs)],
                })
            return {
                "lanes": lanes,
                "history": [self._payload(j) for j in reversed(jobs) if j.status not in PENDING],
                "pending_count": sum(j.status in PENDING for j in jobs),
                "workers": self._worker_snapshot(),
            }

    def _candidate_job(self, entry, preset, result, *, job_id: str,
                       requested_preserve_audio: bool | None,
                       preserve_subtitles_and_metadata: bool, replace_source: bool,
                       video_bitrate: int | None = None) -> QueueJob:
        return QueueJob(
            id=job_id, media_id=entry.item.id, scope=entry.scope,
            name=entry.name, backend=preset.backend, preset=preset.model_copy(deep=True),
            preserve_audio=result.preserve_audio,
            requested_preserve_audio=requested_preserve_audio,
            preserve_subtitles_and_metadata=preserve_subtitles_and_metadata,
            source_size=result.source_size, source_codec=entry.item.video_codec,
            source_file_id=entry.item.id if entry.item.revision_id else None,
            source_revision_id=entry.item.revision_id,
            source_reference=(
                getattr(self.worker, "capture_source_reference", lambda item: None)(entry.item)
            ),
            replace_source=replace_source,
            chosen_video_bitrate=video_bitrate if result.video_bitrate_required else None,
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
                          preserve_subtitles_and_metadata: bool, replace_source: bool = False,
                          video_bitrate: int | None = None) -> list[str]:
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
            preserve_subtitles_and_metadata=preserve_subtitles_and_metadata,
            replace_source=replace_source,
            video_bitrate=video_bitrate,
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
        replacement = self._replacement_checks(request.media_ids, request.scope) if request.replace_source else {}
        with self.lock, self.repository.transaction():
            pending = {(j.media_id, j.scope) for j in self.repository.pending()}
            for media_id in dict.fromkeys(request.media_ids):
                try:
                    entry = self.catalog.find(media_id, request.scope)
                except LookupError as error:
                    excluded.append({"media_id": media_id, "reasons": [str(error)]})
                    continue
                result = self.catalog.evaluate(entry, preset, request.preserve_audio,
                                               request.preserve_subtitles_and_metadata, request.video_bitrate)
                reasons = list(result.reasons)
                if preset.backend not in self.supported_backends:
                    reasons.append(f"{preset.backend.upper()} real encoding is not available in this runtime.")
                if (media_id, request.scope) in pending:
                    reasons.append("Already queued or active.")
                if preset.destination_codec != "hevc":
                    reasons.append("AV1 is not available in this workflow.")
                if reasons:
                    excluded.append({"media_id": media_id, "reasons": reasons})
                    continue
                job = self._candidate_job(
                    entry, preset, result, job_id=str(uuid4()),
                    requested_preserve_audio=request.preserve_audio,
                    preserve_subtitles_and_metadata=result.preserve_subtitles_and_metadata,
                    replace_source=request.replace_source,
                    video_bitrate=request.video_bitrate,
                )
                capability_check = getattr(self.worker, "enqueue_reasons", None)
                if capability_check:
                    reasons.extend(capability_check(entry.item, job, replacement=replacement.get(media_id, [])))
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

    def _replacement_checks(self, media_ids: list[str], scope) -> dict[str, list[str]]:
        """replacement_check() for each item's current source, before a transaction opens."""
        check = getattr(self.worker, "replacement_check", None)
        capture = getattr(self.worker, "capture_source_reference", None)
        if check is None or capture is None:
            return {}
        checks = {}
        for media_id in dict.fromkeys(media_ids):
            try:
                reference = capture(self.catalog.find(media_id, scope).item)
            except LookupError:
                continue  # Excluded with its reason inside the transaction.
            if reference is not None:
                checks[media_id] = check(reference)
        return checks

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

    def skip(self, job_id: str, reason: str = "user_stop") -> None:
        if self._find(job_id).status == "replacing":
            raise QueueConflict("The source is being replaced and cannot be interrupted. "
                                "The job finishes, or rolls back to the original, on its own.")
        if self._find_external_active(job_id):
            # The HTTP process does not own the ffmpeg subprocess. Persist the
            # cancellation request; the standalone worker observes it and stops
            # only its own process.
            with self.lock, self.repository.transaction():
                job = self._find(job_id)
                if job.status == "completed":
                    raise QueueConflict("The job completed before Stop & Skip took effect.")
                if job.status == "replacing":
                    raise QueueConflict("The source is being replaced and cannot be interrupted.")
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
            job.move_to("skipped")
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
        for backend in ("cpu", "gpu"):
            if backend in self.external_backends:
                continue
            if self.controls and not self.controls.can_claim(backend):
                continue
            if any(j.backend == backend and j.status in ACTIVE for j in self.repository.pending()):
                continue
            queued = self._queued(backend)
            if queued:
                job = queued[0]
                job.move_to("encoding")
                job.started_at = now()
                self.repository.save(job)

    def tick(self, seconds: float) -> None:
        """Called by the fake worker clock, never by GET requests."""
        with self.lock, self.repository.transaction():
            self.revalidate()
            for job in self.repository.pending():
                if job.backend in self.external_backends:
                    continue
                if job.status in ACTIVE:
                    self.worker.advance(job, seconds)
                    if job.status == "completed":
                        job.finished_at = now()
                    self.repository.save(job)
            self._start_idle_lanes()

    def claim_next(self, backend: str) -> QueueJob | None:
        if backend not in self.external_backends or backend not in self.supported_backends:
            return None
        if self.controls and not self.controls.can_claim(backend):
            return None
        if monotonic() < self._claim_backoff.get(backend, 0.0):
            return None
        # Idle workers poll frequently. Avoid BEGIN IMMEDIATE when there is
        # visibly no queued work; the transactional section below rechecks all
        # conditions before claiming, so this preflight cannot create a race.
        if not self._queued(backend):
            return None
        storage = self.storage_checks({backend})
        with self.lock, self.repository.transaction():
            if self.controls and not self.controls.can_claim(backend):
                return None
            if any(j.backend == backend and j.status in ACTIVE for j in self.repository.pending()):
                return None
            self.revalidate(backends={backend}, storage=storage)
            queued = self._queued(backend)
            if not queued:
                return None
            # Jobs whose library root is unreachable wait; the rest of the lane runs.
            claimable = [j for j in queued if not self._awaits_root(j, storage.unavailable_roots)]
            waiting = not claimable
            if waiting != (backend in self._lanes_waiting):
                # Log the change, not every poll: an outage can last for hours.
                if waiting:
                    self._lanes_waiting.add(backend)
                    log.warning("%s lane: all %d queued sources are on unavailable library roots; waiting.",
                                backend.upper(), len(queued))
                else:
                    self._lanes_waiting.discard(backend)
                    log.info("%s lane: queued sources are reachable again; claiming.", backend.upper())
            if waiting:
                self._claim_backoff[backend] = monotonic() + OUTAGE_BACKOFF_SECONDS
                return None
            job = claimable[0]
            job.move_to("encoding")
            job.started_at = now()
            self.repository.save(job)
            return job.model_copy(deep=True)

    def revalidate(self, *, defer_external_stops: bool = False,
                   backends: set[str] | frozenset[str] | None = None,
                   storage: StorageChecks | None = None) -> list[str]:
        """Apply current protections, optionally restricted to process-owned lanes.

        A caller already inside a transaction passes storage, gathered before it
        opened that transaction (see storage_checks()).
        """
        if storage is None:
            storage = self.storage_checks(backends)
        stop_after_commit = []
        with self.lock, self.repository.transaction():
            for job in self.repository.pending():
                if backends is not None and job.backend not in backends:
                    continue
                if job.status not in PENDING or job.status == "replacing":
                    continue  # A source swap is never interrupted by policy changes.
                if self._awaits_root(job, storage.unavailable_roots):
                    # A dropped mount is not a verdict: re-check once the root is back.
                    if WAITING_FOR_ROOT not in job.reasons:
                        job.reasons = [*job.reasons, WAITING_FOR_ROOT]
                        self.repository.save(job)
                    continue
                before = job.model_dump()
                result = None
                try:
                    entry = self.catalog.find(job.media_id, job.scope)
                    # The job's own request decides; None means preserve audio.
                    result = self.catalog.evaluate(entry, job.preset,
                        job.requested_preserve_audio, job.preserve_subtitles_and_metadata, job.chosen_video_bitrate)
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
                    # Existing jobs remain queued while a CPU/GPU worker is
                    # temporarily unavailable and resume when that lane returns.
                    if job.status in ACTIVE and result.preserve_audio != job.preserve_audio:
                        reasons.append("Audio policy changed during execution. Submit a new job.")
                    capability_check = getattr(self.worker, "enqueue_reasons", None)
                    if capability_check and job.status == "queued":
                        # A replace job queued after storage was checked gets checked on
                        # the next pass, and again before its swap; never on disk here.
                        capability_reasons = capability_check(
                            entry.item, job, replacement=storage.replacement.get(job.id, []))
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
                    job.move_to("blocked")
                    job.reasons = reasons
                    job.finished_at = now()
                elif job.status == "queued":
                    assert result is not None
                    job.reasons = [reason for reason in job.reasons if reason != WAITING_FOR_ROOT]
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
