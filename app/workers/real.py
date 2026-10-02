"""Standalone real encoder worker with one owned backend lane per process."""
import logging
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Literal, TypeVar

from app.models.inventory import SourceReference
from app.models.media import Episode, Movie
from app.models.queue import QueueJob
from app.services.encoding_capability import CPUEncodeCapability, GPUEncodeCapability, MediaItem
from app.services.analysis import AnalysisTarget
from app.services.errors import Conflict
from app.services.source_guard import SourceGuard
from app.services.filesystem_source import FilesystemObservationSource
from app.workers.ffmpeg import EncodingCancelled, FFmpegEncoder, FFmpegError
from app.workers.replacement import ReplacementError, SourceReplacer, replacement_reasons

if TYPE_CHECKING:
    from app.services.queue import QueueService
    from app.services.catalog import CatalogService
    from app.services.ffprobe import TechnicalProbe


Backend = Literal["cpu", "gpu"]
T = TypeVar("T")
PERSIST_ATTEMPTS = 5


def _primary_video(item: MediaItem):
    if item.probe is None:
        return None
    return next((s for s in item.probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)
log = logging.getLogger(__name__)


class RealEncoderWorker:
    """Owns at most one real backend lane and never crosses process ownership."""

    filesystem_mode = True
    execution_mode = "real"
    external_backends = frozenset({"cpu", "gpu"})

    def __init__(self, *, backend: Backend | None, supported_backends: set[str] | frozenset[str],
                 unavailable_reason: str | None, encoder: FFmpegEncoder,
                 source: FilesystemObservationSource, guard: SourceGuard,
                 capabilities: dict[str, CPUEncodeCapability | GPUEncodeCapability],
                 probe: "TechnicalProbe", replacer: SourceReplacer | None = None):
        self.backend = backend
        self.runtime_backends = frozenset(supported_backends)
        self.supported_backends = (
            self.runtime_backends
            if backend is None
            else frozenset({backend}) if backend in self.runtime_backends else frozenset()
        )
        self.enabled = bool(self.supported_backends)
        self.unavailable_reason = unavailable_reason
        self.encoder = encoder
        self.catalog: CatalogService | None = None
        self.source = source
        self.guard = guard
        self.capabilities = capabilities
        self.probe = probe
        self.replacer = replacer or SourceReplacer()
        self.queue: QueueService | None = None
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._control_thread: Thread | None = None
        self._cancelled: set[str] = set()
        self._active: set[str] = set()

    def diagnostics(self, runtime: dict) -> dict:
        supported = list(runtime.get("supported_backends", self.supported_backends))
        modes = []
        if "cpu" in supported:
            modes.append("CPU · x265")
        if "gpu" in supported:
            modes.append("GPU · Intel VA-API")
        return {
            **runtime,
            "encoding_enabled": bool(supported),
            "encoder_mode": " + ".join(modes) + " · SDR · keep-output" if modes else "disabled",
            "supported_backends": supported,
        }

    def _capability(self, backend: str):
        capability = self.capabilities.get(backend)
        if capability is None:
            raise Conflict(f"No real execution capability is registered for {backend.upper()}.")
        return capability

    def enqueue_reasons(self, item: MediaItem, job: QueueJob) -> list[str]:
        reasons = self._capability(job.backend).reasons(item, job)
        reference = job.source_reference
        revision_id = item.revision_id
        if reference is not None and revision_id is not None and revision_id == reference.revision_id:
            current = self.source.reference_for(item.id, revision_id)
            if current != reference:
                reasons.append("Source path or physical identity changed after the job was queued.")
        elif reference is not None:
            reasons.append("Source revision changed after the job was queued.")
        if job.replace_source and reference is not None:
            path = self.source.path_for(reference)
            if path is None:
                reasons.append("Current source path is unavailable.")
            else:
                reasons.extend(replacement_reasons(path))
        return list(dict.fromkeys(reasons))

    def recover_replacement(self, job: QueueJob) -> tuple[str, str]:
        """Resolve a replacement interrupted by a worker crash or kill.

        A swap that had already been verified is completed; anything earlier is
        rolled back to the original. Unknown states are left untouched.
        """
        assert job.replacement is not None
        return self.replacer.resolve(job.replacement, finalize=True)

    def capture_source_reference(self, item: Movie | Episode) -> SourceReference | None:
        if item.revision_id is None:
            return None
        return self.source.reference_for(item.id, item.revision_id)

    def cleanup_interrupted(self, job_id: str) -> None:
        self.encoder.cleanup_job_directory(job_id)

    def bind(self, queue: "QueueService", catalog: "CatalogService") -> None:
        self.queue = queue
        self.catalog = catalog

    def start(self) -> None:
        if not self.enabled or self.backend is None or self._thread is not None:
            return
        backend = self.backend
        self._thread = Thread(target=self._run, name=f"kompressor-{backend}-encoder", daemon=True)
        self._control_thread = Thread(target=self._control_loop, name=f"kompressor-{backend}-control", daemon=True)
        self._thread.start()
        self._control_thread.start()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _control_loop(self) -> None:
        backend = self.backend
        if backend is None:
            return
        last_quiet: bool | None = None
        while not self._stop.wait(0.5):
            queue = self.queue
            if queue is None:
                continue
            try:
                quiet = queue.quiet_active(backend)
                if quiet and last_quiet is not True:
                    queue.enforce_quiet_start(backend)
                last_quiet = quiet
                with self._lock:
                    active = list(self._active)
                for job_id in active:
                    if queue.cancel_requested(job_id):
                        self.stop(job_id)
            except Exception:
                log.exception("%s worker control loop iteration failed", backend.upper())

    def _run(self) -> None:
        backend = self.backend
        if backend is None:
            return
        while not self._stop.is_set():
            queue = self.queue
            if backend == "gpu":
                device = self.encoder.gpu_device
                ready = device is not None and self.encoder.gpu_device_check(device)[0]
                if not ready:
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
            try:
                job = queue.claim_next(backend) if queue else None
            except Exception:
                log.exception("%s worker could not claim the next job", backend.upper())
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if job is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            job_id = job.id
            with self._lock:
                self._active.add(job_id)
            try:
                self._execute(job)
            except EncodingCancelled:
                if not self._stop.is_set() and queue:
                    owner = queue
                    self._persist("record the stop", lambda: owner.cancelled_real_job(job_id))
            except Exception as error:
                if queue:
                    owner = queue
                    self._persist("record the failure", lambda: owner.fail_real_job(
                        job_id, str(error), getattr(error, "exit_code", None)))
                self.encoder.cleanup(job.id, remove_final=True)
            finally:
                self.encoder.cleanup(job.id)
                with self._lock:
                    self._active.discard(job.id)
                    self._cancelled.discard(job.id)
                self._wake.set()

    @staticmethod
    def _persist(what: str, action: Callable[[], T]) -> T | None:
        """Retry a job-state write (e.g. SQLite briefly locked by the web process).

        Never raises: an escaped error here would end the lane thread and leave
        the job active until a restart, which then recovers it.
        """
        for attempt in range(1, PERSIST_ATTEMPTS + 1):
            try:
                return action()
            except Exception:
                if attempt == PERSIST_ATTEMPTS:
                    log.exception("Could not %s after %d attempts; restart recovery will resolve the job.",
                                  what, attempt)
                    return None
                time.sleep(attempt)
        return None

    def _execute(self, job: QueueJob) -> None:
        queue = self.queue
        if queue is None:
            raise RuntimeError("Real queue worker is not bound.")
        if self.backend is not None and job.backend != self.backend:
            raise Conflict(f"{self.backend.upper()} worker cannot execute a {job.backend.upper()} job.")
        with self._lock:
            if job.id in self._cancelled or self._stop.is_set():
                raise EncodingCancelled("Encoding stopped before execution.")
        if not job.source_file_id or not job.source_revision_id:
            raise Conflict("Job has no captured filesystem file/revision identity.")
        if job.media_id != job.source_file_id:
            raise Conflict("Job media ID no longer identifies its captured file.")
        reference = job.source_reference
        if (reference is None or reference.file_id != job.source_file_id
                or reference.revision_id != job.source_revision_id):
            raise Conflict("Job has no matching captured source identity.")

        catalog = self.catalog
        if catalog is None:
            raise RuntimeError("Real worker catalog is not bound.")
        entry = catalog.find(job.media_id, job.scope)
        result = catalog.evaluate(entry, job.preset, job.requested_preserve_audio, job.preserve_subtitles,
                                  job.chosen_video_bitrate)
        if not result.eligible:
            raise Conflict("Policy changed before execution: " + "; ".join(result.reasons))
        if result.preserve_audio != job.preserve_audio:
            raise Conflict("Audio policy changed before execution. Submit a new job.")

        item = entry.item
        capability = self._capability(job.backend)
        reasons = capability.reasons(item, job)
        if reasons:
            raise Conflict("Real execution refused: " + "; ".join(reasons))
        source_path = self.source.path_for(reference)
        if source_path is None:
            raise Conflict("Current source path is unavailable.")
        if item.probe is None:
            raise Conflict("Source technical metadata is unavailable.")
        if job.reuse_output_path is not None:
            self._replace_with_kept_output(job, reference, item, capability, source_path)
            return

        output = self.encoder.encode(
            job, source_path, item.probe,
            lambda percent, elapsed: queue.update_real_progress(job.id, percent, elapsed),
            before_start=lambda: self.guard.before_processing(reference),
        )
        if not queue.set_real_validating(job.id, str(output.final_path)):
            self.encoder.cleanup(job.id, remove_final=True)
            raise EncodingCancelled("Job was stopped before output validation.")

        output_probe = self.probe.inspect(output.partial_path)
        with self._lock:
            if job.id in self._cancelled or self._stop.is_set():
                raise EncodingCancelled("Encoding stopped during output validation.")
        errors = capability.validate_output(item, job, output_probe, output.partial_path)
        if not errors:
            # Reads source and output in full: every track planned as copied must be
            # bit-identical, or the job fails and nothing is promoted or replaced.
            errors = self.encoder.copied_audio_mismatches(
                job, source_path, item.probe, output.partial_path, output_probe)
        if errors:
            queue.record_validation_errors(job.id, errors)
            raise Conflict("Output validation failed: " + "; ".join(errors))

        with self._lock:
            if job.id in self._cancelled or self._stop.is_set():
                raise EncodingCancelled("Encoding stopped before output promotion.")
            self.guard.before_processing(reference)
            final_path = self.encoder.promote(job.id, output)
            size = final_path.stat().st_size
            if not job.replace_source:
                if not queue.complete_real_job(job.id, str(final_path), size):
                    self.encoder.cleanup(job.id, remove_final=True)
                    raise EncodingCancelled("Job was stopped before completion was persisted.")
                return
        self._replace_source(job, reference, item, capability, source_path, final_path, size)

    def _replace_with_kept_output(self, job: QueueJob, reference: SourceReference, item: MediaItem,
                                  capability, source_path: Path) -> None:
        """History "Replace source": the full output checks again, then the normal swap."""
        queue = self.queue
        assert queue is not None and item.probe is not None
        assert job.reuse_output_path is not None and job.replaces_job_id is not None
        output = self.encoder.kept_output(job.replaces_job_id, job.reuse_output_path)
        if not queue.set_real_validating(job.id, str(output)):
            raise EncodingCancelled("Job was stopped before output validation.")
        output_probe = self.probe.inspect(output)
        with self._lock:
            if job.id in self._cancelled or self._stop.is_set():
                raise EncodingCancelled("Stopped during output validation.")
        errors = capability.validate_output(item, job, output_probe, output)
        if not errors:
            errors = self.encoder.copied_audio_mismatches(job, source_path, item.probe, output, output_probe)
        if errors:
            queue.record_validation_errors(job.id, errors)
            raise Conflict("Kept output failed validation: " + "; ".join(errors))
        self.guard.before_processing(reference)
        self._replace_source(job, reference, item, capability, source_path, output, output.stat().st_size)

    def analysis_target(self, job: QueueJob, item: MediaItem) -> AnalysisTarget:
        """Read-only pair for comparisons: only while the source is still the encoded revision."""
        assert job.output_path is not None
        try:
            output = self.encoder.kept_output(job.id, job.output_path)
        except FFmpegError as error:
            raise Conflict(str(error)) from error
        reference = job.source_reference
        if reference is None or self.source.reference_for(item.id, reference.revision_id) != reference:
            raise Conflict("The source changed since this output was encoded; comparisons would be meaningless.")
        source = self.source.path_for(reference)
        video = _primary_video(item)
        if source is None or video is None or item.duration_seconds is None:
            raise Conflict("The source file, its video stream or its duration is unavailable.")
        return AnalysisTarget(job_id=job.id, name=job.name, source=source, output=output,
                              duration=item.duration_seconds, video=video)

    def discard_kept_output(self, job: QueueJob) -> None:
        """Remove a kept output once it has been moved into its source (by copy)."""
        if job.reuse_output_path is None or job.replaces_job_id is None:
            return
        try:
            self.encoder.kept_output(job.replaces_job_id, job.reuse_output_path).unlink()
        except (OSError, FFmpegError):
            pass

    def _replace_source(self, job: QueueJob, reference: SourceReference, item: MediaItem,
                        capability, source_path: Path, output_path: Path, size: int) -> None:
        queue = self.queue
        assert queue is not None
        reused = job.reuse_output_path is not None
        saving = job.source_size - size
        percent = saving / job.source_size * 100 if job.source_size else 0.0
        minimum = job.preset.minimum_expected_saving_percent
        # A History replacement is an explicit choice after seeing the measured
        # result, so only "must be smaller" applies; encodes use the preset minimum.
        if saving <= 0 or (not reused and percent < minimum):
            if reused:
                reason = (f"The kept output is not smaller than the source ({percent:.1f}% saving). "
                          "The source and the kept output were left unchanged.")
            else:
                self.encoder.cleanup(job.id, remove_final=True)
                reason = (f"Measured saving {percent:.1f}% is below the preset minimum of {minimum:g}%. "
                          "The source was kept and the output discarded.")
            if not queue.skip_real_job(job.id, size, reason):
                raise EncodingCancelled("Job was stopped before the saving check was persisted.")
            return
        journal = self.replacer.plan(job.id, source_path, size)
        # From here the job cannot be stopped: Stop & Skip is refused while replacing.
        if not queue.set_real_replacing(job.id, journal):
            if not reused:
                self.encoder.cleanup(job.id, remove_final=True)
            raise EncodingCancelled("Job was stopped before source replacement.")

        def verify(path: Path) -> list[str]:
            return capability.validate_output(item, job, self.probe.inspect(path), path)

        try:
            notes = self.replacer.replace(
                journal, output_path,
                before_swap=lambda: self.guard.before_replacement(reference),
                verify=verify, save=lambda state: queue.update_replacement(job.id, state),
                should_abort=self._stop.is_set)
        except ReplacementError:
            if self._stop.is_set():
                # The original is untouched or restored. Leave the job "replacing"
                # so restart recovery keeps the validated output (see QueueService).
                raise EncodingCancelled("Worker stopped during source replacement.")
            raise
        # The source is replaced now; failing the job would misreport it.
        completed = self._persist("record the completed replacement", lambda: queue.complete_real_job(
            job.id, str(source_path), size, source_replaced=True, notes=notes))
        if completed is None:
            return
        if not completed:
            raise RuntimeError("The source was replaced, but completion could not be recorded.")
        if reused:
            assert job.replaces_job_id is not None
            self.discard_kept_output(job)
            queue.mark_output_used(job.replaces_job_id, job.id)
        else:
            self.encoder.cleanup(job.id, remove_final=True)

    def advance(self, job: QueueJob, seconds: float) -> None:
        raise RuntimeError("The real worker is driven by its background thread, not fake ticks.")

    def stop(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)
        self.encoder.stop(job_id)
        self._wake.set()

    def cancelled_by_request(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            jobs = list(self._active)
            self._cancelled.update(jobs)
        for job_id in jobs:
            self.encoder.stop(job_id)
        self.encoder.stop_all()
        if self._thread:
            self._thread.join()
        if self._control_thread:
            self._control_thread.join()
