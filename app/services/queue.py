"""Dual-lane scheduler. Policy decisions remain in the existing PolicyEngine."""
import logging
from pathlib import Path
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from app.services.errors import Conflict, InvalidOperation, NotFound
from app.models.queue import EnqueueRequest, Priority, QueueJob, ReplacementJournal
from app.models.preferences import WorkerSettings
from app.repositories.base import QueueRepository
from app.services.catalog import CatalogService
from app.services.worker_control import WorkerControlService
from app.workers.base import EncoderWorker


# "replacing" is active but never stoppable: the source swap must run to a known state.
ACTIVE = {"encoding", "validating", "replacing", "stopping"}
PENDING = {"queued", *ACTIVE}
PRIORITIES = {"urgent": 3, "high": 2, "normal": 1, "low": 0}
# How long a lane whose queued sources are all unreachable waits before looking again.
OUTAGE_BACKOFF_SECONDS = 10.0

log = logging.getLogger(__name__)


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
        self.execution_mode: Literal["fake", "real"] = (
            "real" if getattr(worker, "filesystem_mode", False) else "fake"
        )
        self.external_backends = frozenset(getattr(worker, "external_backends", ()))
        self.supported_backends = frozenset(getattr(worker, "supported_backends", ("cpu", "gpu")))
        self.lock = RLock()
        self._claim_backoff: dict[str, float] = {}
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

    def _source_outage(self, job: QueueJob) -> str | None:
        check = getattr(self.worker, "source_outage", None)
        return check(job) if check and job.execution_mode == "real" else None

    def _queued(self, backend: str, jobs: list[QueueJob] | None = None) -> list[QueueJob]:
        source = self.repository.get_all() if jobs is None else jobs
        return sorted(
            (j for j in source if j.backend == backend and j.status == "queued"),
            key=lambda j: (-j.move_next_order, -PRIORITIES[j.priority],
                           -(j.estimated_saving if j.estimated_saving is not None else j.planning_saving or 0),
                           j.created_at, j.id),
        )

    def _worker_snapshot(self) -> dict | None:
        if self.controls is None:
            return None
        snapshot = self.controls.snapshot()
        for backend, lane in snapshot["lanes"].items():
            available = backend in self.supported_backends
            lane["available"] = available
            lane["accepting_jobs"] = bool(lane["accepting_jobs"] and available)
        return snapshot

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
                       preserve_subtitles: bool, replace_source: bool,
                       video_bitrate: int | None = None) -> QueueJob:
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
                          preserve_subtitles: bool, replace_source: bool = False,
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
            preserve_subtitles=preserve_subtitles,
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
        with self.lock, self.repository.transaction():
            for media_id in dict.fromkeys(request.media_ids):
                try:
                    entry = self.catalog.find(media_id, request.scope)
                except LookupError as error:
                    excluded.append({"media_id": media_id, "reasons": [str(error)]})
                    continue
                result = self.catalog.evaluate(entry, preset, request.preserve_audio,
                                               request.preserve_subtitles, request.video_bitrate)
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
                    video_bitrate=request.video_bitrate,
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

    def enqueue_replacement(self, job_id: str) -> dict:
        """Queue a replace-only job that swaps a kept, validated output into its source.

        The lane worker re-validates the kept output (including the bit-exact audio
        check) and re-checks the source before the same journalled swap a replace
        encode uses. Nothing is re-encoded.
        """
        if not getattr(self.worker, "filesystem_mode", False):
            raise InvalidOperation("Replacing from History needs real filesystem encoding.")
        if not getattr(self.worker, "enabled", False):
            raise InvalidOperation("Real encoding is unavailable: " + (
                getattr(self.worker, "unavailable_reason", None) or "runtime prerequisites failed."))
        with self.lock, self.repository.transaction():
            kept = self._find(job_id)
            if (kept.execution_mode != "real" or kept.status != "completed" or kept.replace_source
                    or kept.source_replaced or not kept.output_path):
                raise QueueConflict("Only completed keep-original real encodes with a kept output can replace their source.")
            jobs = self.repository.get_all()
            if any(j.media_id == kept.media_id and j.scope == kept.scope and j.status in PENDING for j in jobs):
                raise QueueConflict("This item already has a queued or active job.")
            if kept.backend not in self.supported_backends:
                raise QueueConflict(f"The {kept.backend.upper()} worker lane is not available.")
            job = kept.model_copy(deep=True, update={
                "id": str(uuid4()), "status": "queued", "created_at": now(), "started_at": None,
                "finished_at": None, "progress": 0, "progress_known": False, "elapsed_seconds": 0,
                "reasons": [], "output_path": None, "output_size": None, "measured_saving": None,
                "error_message": None, "ffmpeg_exit_code": None, "validation_errors": [],
                "cancel_requested": False, "cancel_reason": None, "replacement": None,
                "source_replaced": False, "move_next_order": 0, "replace_source": True,
                "reuse_output_path": kept.output_path, "replaces_job_id": kept.id,
            })
            entry = self.catalog.find(job.media_id, job.scope)
            result = self.catalog.evaluate(entry, job.preset, job.requested_preserve_audio, job.preserve_subtitles,
                                           job.chosen_video_bitrate)
            reasons = list(result.reasons)
            if result.preserve_audio != job.preserve_audio:
                reasons.append("The audio policy changed since this output was encoded; encode again instead.")
            capability_check = getattr(self.worker, "enqueue_reasons", None)
            if capability_check:
                reasons.extend(capability_check(entry.item, job))
            if reasons:
                raise QueueConflict("Cannot replace the source: " + " ".join(dict.fromkeys(reasons)))
            self.repository.add(job)
        wake = getattr(self.worker, "wake", None)
        if wake:
            wake()
        return job.model_dump()

    def analysis_target(self, job_id: str):
        """Source/output pair of a kept keep-original output, for History quality tools."""
        job = self._find(job_id)
        if (job.execution_mode != "real" or job.status != "completed" or job.replace_source
                or job.source_replaced or not job.output_path):
            raise QueueConflict("Comparisons need a completed keep-original encode whose output is still kept.")
        resolve = getattr(self.worker, "analysis_target", None)
        if resolve is None:
            raise InvalidOperation("Comparisons need real filesystem encoding.")
        return resolve(job, self.catalog.find(job.media_id, job.scope).item)

    def record_analysis(self, job_id: str, kind: str, result: dict) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if kind == "comparison":
                job.comparison = result
            elif kind == "vmaf":
                job.vmaf = result
            else:
                raise InvalidOperation(f"Unknown analysis kind: {kind}")
            self.repository.save(job)

    def mark_output_used(self, kept_job_id: str, replacement_job_id: str) -> None:
        with self.lock, self.repository.transaction():
            kept = self._find(kept_job_id)
            kept.output_path = None
            kept.reasons = [*kept.reasons, "This kept output was moved into the source from History."]
            self.repository.save(kept)

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

    def delete_history(self, job_id: str) -> None:
        """Forget a finished job. A kept output still in the workspace goes with it."""
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status in PENDING:
                raise QueueConflict("Only finished jobs can be deleted from History.")
            if any(j.replaces_job_id == job.id and j.status in PENDING for j in self.repository.get_all()):
                raise QueueConflict("A replacement using this kept output is queued or running.")
            self.repository.remove(job_id)
        cleanup = getattr(self.worker, "cleanup_interrupted", None)
        if cleanup and job.execution_mode == "real":
            # Only Kompressor's own outputs in <workspace>/jobs/<id>; never the source.
            cleanup(job.id)

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
        for backend in ("cpu", "gpu"):
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

    def _recover_replacements(self, backends: set[str] | frozenset[str] | None) -> None:
        """Resolve interrupted source swaps before any other recovery.

        Filesystem work happens outside the database transaction. A job is never
        requeued from here: the original is either untouched/restored (failed,
        resubmit), the verified replacement is in place (completed), or the state
        is unknown and left for manual review (failed, nothing changed).
        """
        resolve = getattr(self.worker, "recover_replacement", None)
        if resolve is None:
            return
        keep_outputs: set[str] = set()
        for job in self.repository.get_all():
            if job.status != "replacing" or (backends is not None and job.backend not in backends):
                continue
            if job.replacement is None:
                outcome, detail = "untouched", "No replacement step had been recorded."
            else:
                outcome, detail = resolve(job)
            with self.lock, self.repository.transaction():
                current = self._find(job.id)
                if current.status != "replacing":
                    continue
                current.finished_at = now()
                if outcome == "replaced":
                    current.status = "completed"
                    current.source_replaced = True
                    current.output_path = current.replacement.target if current.replacement else None
                    current.output_size = current.replacement.output_size if current.replacement else None
                    current.measured_saving = (current.source_size - current.output_size
                                               if current.output_size is not None else None)
                    current.reasons = [*current.reasons, "Completed after a worker restart. " + detail]
                elif outcome != "manual" and self._keepable_output(current):
                    # A stop during the copy must not throw away a validated encode:
                    # keep it as a keep-original result that History can swap in.
                    assert current.output_path is not None
                    current.status = "completed"
                    current.replace_source = False
                    current.output_size = Path(current.output_path).stat().st_size
                    current.measured_saving = current.source_size - current.output_size
                    current.reasons = [*current.reasons, "Worker stopped during source replacement. " + detail
                                       + " The validated output was kept; use Replace source in History."]
                    keep_outputs.add(current.id)
                else:
                    current.status = "failed"
                    current.output_path = None
                    current.output_size = None
                    current.measured_saving = None
                    current.error_message = ("Worker stopped during source replacement. " + detail
                                             + ("" if outcome == "manual" else " Submit the job again."))
                self.repository.save(current)
            if outcome == "replaced" and job.reuse_output_path and job.replaces_job_id:
                discard = getattr(self.worker, "discard_kept_output", None)
                if discard:
                    discard(job)
                self.mark_output_used(job.replaces_job_id, job.id)
            if outcome != "manual" and job.id not in keep_outputs:
                cleanup = getattr(self.worker, "cleanup_interrupted", None)
                if cleanup:
                    cleanup(job.id)

    @staticmethod
    def _keepable_output(job: QueueJob) -> bool:
        # History replacements reuse another job's kept output, which stays with that job.
        return (job.reuse_output_path is None and job.output_path is not None
                and Path(job.output_path).is_file())

    def recover(self, backends: set[str] | frozenset[str] | None = None) -> None:
        """Recover only lanes owned by this process; seed recovery still covers all lanes."""
        self._recover_replacements(backends)
        interrupted = []
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if backends is not None and job.backend not in backends:
                    continue
                if job.status == "replacing":
                    continue  # Only reachable without a replacement-aware worker; never requeue a swap.
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
        with self.lock, self.repository.transaction():
            if self.controls and not self.controls.can_claim(backend):
                return None
            if any(j.backend == backend and j.status in ACTIVE for j in self.repository.get_all()):
                return None
            self.revalidate(backends={backend})
            queued = self._queued(backend)
            if not queued:
                return None
            # Jobs whose library root is unreachable wait; the rest of the lane runs.
            claimable = [j for j in queued if not self._source_outage(j)]
            if not claimable:
                self._claim_backoff[backend] = monotonic() + OUTAGE_BACKOFF_SECONDS
                log.warning("%s lane: all %d queued sources are on unavailable library roots; waiting.",
                            backend.upper(), len(queued))
                return None
            job = claimable[0]
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

    def set_real_replacing(self, job_id: str, journal: ReplacementJournal) -> bool:
        """Last point at which a stop request wins; afterwards the swap is not stoppable."""
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "validating" or job.cancel_requested or not job.replace_source:
                return False
            job.status = "replacing"
            job.replacement = journal
            self.repository.save(job)
            return True

    def update_replacement(self, job_id: str, journal: ReplacementJournal) -> None:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "replacing":
                raise QueueConflict("Replacement journal update for a job that is not replacing.")
            job.replacement = journal.model_copy(deep=True)
            self.repository.save(job)

    def complete_real_job(self, job_id: str, output_path: str, output_size: int, *,
                          source_replaced: bool = False, notes: list[str] | None = None) -> bool:
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            expected = "replacing" if source_replaced else "validating"
            if job.status != expected or job.cancel_requested:
                return False
            job.status = "completed"
            job.progress = 100
            job.finished_at = now()
            job.output_path = output_path
            job.output_size = output_size
            job.measured_saving = job.source_size - output_size
            job.source_replaced = source_replaced
            job.reasons = [*job.reasons, *(notes or [])]
            job.error_message = None
            job.cancel_requested = False
            job.cancel_reason = None
            self.repository.save(job)
            return True

    def skip_real_job(self, job_id: str, output_size: int, reason: str) -> bool:
        """A validated replace job whose measured saving does not justify replacement."""
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "validating" or job.cancel_requested:
                return False
            job.status = "skipped"
            job.finished_at = now()
            job.output_path = None
            job.output_size = output_size
            job.measured_saving = job.source_size - output_size
            job.reasons = [*job.reasons, reason]
            self.repository.save(job)
            return True

    def keep_real_output(self, job_id: str, output_path: str, output_size: int, reason: str) -> bool:
        """A replace job whose swap failed with the original intact keeps its validated output.

        The job becomes a keep-original result, exactly as restart recovery records
        one, so History offers Replace source for it.
        """
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status != "replacing":
                return False
            job.status = "completed"
            job.replace_source = False
            job.progress = 100
            job.finished_at = now()
            job.output_path = output_path
            job.output_size = output_size
            job.measured_saving = job.source_size - output_size
            job.reasons = [*job.reasons, reason]
            job.error_message = None
            job.cancel_requested = False
            job.cancel_reason = None
            self.repository.save(job)
            return True

    def requeue_real_job(self, job_id: str, reason: str) -> bool:
        """Put a job interrupted by a storage outage back in the queue, from zero."""
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status not in {"encoding", "validating"} or job.cancel_requested:
                return False
            job.status = "queued"
            job.progress = 0
            job.progress_known = False
            job.elapsed_seconds = 0
            job.started_at = None
            job.output_path = None
            job.output_size = None
            job.measured_saving = None
            job.error_message = None
            job.ffmpeg_exit_code = None
            job.validation_errors = []
            job.reasons = list(dict.fromkeys([*job.reasons, reason]))
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
        # This is polled by the owning worker control thread. A read snapshot is
        # sufficient and avoids taking SQLite's writer lock every 500 ms.
        with self.lock:
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
            if active.status == "replacing":
                return "finish"
            action = self.controls.quiet_action(backend, active)
            if action == "stop" and active.backend in self.external_backends:
                self._request_cancel(active, "quiet_hours_cutoff")
                self.repository.save(active)
            return action

    def get_worker_settings(self) -> dict:
        snapshot = self._worker_snapshot()
        if snapshot is not None:
            return snapshot
        return {
            "timezone": "UTC", "lanes": {
                backend: {"paused": False, "quiet_hours_enabled": False,
                          "quiet_start": "18:00", "quiet_end": "01:00",
                          "quiet_cutoff_percent": 50.0, "quiet_active": False,
                          "available": True, "accepting_jobs": True}
                for backend in ("cpu", "gpu")
            }}

    def update_worker_settings(self, settings: WorkerSettings) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        with self.lock:
            current = self.controls.get()
            settings = settings.model_copy(update={
                "cpu": settings.cpu.model_copy(update={"paused": current.cpu.paused}),
                "gpu": settings.gpu.model_copy(update={"paused": current.gpu.paused}),
            })
            self.controls.save(settings)
            snapshot = self._worker_snapshot()
            assert snapshot is not None
        wake = getattr(self.worker, "wake", None)
        if wake:
            wake()
        return snapshot

    def set_worker_paused(self, backend: str, paused: bool) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        if backend not in {"cpu", "gpu"}:
            raise InvalidOperation("Unknown worker backend.")
        with self.lock:
            self.controls.set_paused(backend, paused)
            snapshot = self._worker_snapshot()
            assert snapshot is not None
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
                for backend in ("cpu", "gpu"):
                    self.stop_active_backend(backend, reason="user_stop")
            snapshot = self._worker_snapshot()
            assert snapshot is not None
        return snapshot

    def resume_all_workers(self) -> dict:
        if self.controls is None:
            raise InvalidOperation("Worker controls are unavailable.")
        with self.lock:
            self.controls.resume_all()
            snapshot = self._worker_snapshot()
            assert snapshot is not None
        wake = getattr(self.worker, "wake", None)
        if wake:
            wake()
        return snapshot

    def stop_active_backend(self, backend: str, reason: str = "user_stop") -> bool:
        with self.lock, self.repository.transaction():
            active = next((j for j in self.repository.get_all()
                           if j.backend == backend and j.status in ACTIVE), None)
            job_id = active.id if active and active.status != "replacing" else None
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
                if job.status not in PENDING or job.status == "replacing":
                    continue  # A source swap is never interrupted by policy changes.
                if job.status == "queued" and self._source_outage(job):
                    # A dropped mount is not a verdict: re-check once the root is back.
                    continue
                before = job.model_dump()
                result = None
                try:
                    entry = self.catalog.find(job.media_id, job.scope)
                    # The job's own request decides; None means preserve audio.
                    result = self.catalog.evaluate(entry, job.preset,
                        job.requested_preserve_audio, job.preserve_subtitles, job.chosen_video_bitrate)
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
