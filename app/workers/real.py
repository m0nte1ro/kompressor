"""Standalone real encoder worker with one owned backend lane per process."""
import logging
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Literal

from app.models.inventory import SourceReference
from app.models.media import Episode, Movie
from app.models.queue import QueueJob
from app.services.encoding_capability import CPUEncodeCapability, QSVEncodeCapability, MediaItem
from app.services.errors import Conflict
from app.services.source_guard import SourceGuard
from app.services.filesystem_source import FilesystemObservationSource
from app.workers.ffmpeg import EncodingCancelled, FFmpegEncoder

if TYPE_CHECKING:
    from app.services.queue import QueueService
    from app.services.catalog import CatalogService
    from app.services.ffprobe import TechnicalProbe


Backend = Literal["cpu", "qsv"]
log = logging.getLogger(__name__)


class RealEncoderWorker:
    """Owns at most one real backend lane and never crosses process ownership."""

    filesystem_mode = True
    execution_mode = "real"
    external_backends = frozenset({"cpu", "qsv"})

    def __init__(self, *, backend: Backend | None, supported_backends: set[str] | frozenset[str],
                 unavailable_reason: str | None, encoder: FFmpegEncoder,
                 source: FilesystemObservationSource, guard: SourceGuard,
                 capabilities: dict[str, CPUEncodeCapability | QSVEncodeCapability],
                 probe: "TechnicalProbe"):
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
            modes.append("CPU · libx265")
        if "qsv" in supported:
            modes.append("Intel QSV · hevc_qsv")
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
        if reference is not None and item.revision_id == reference.revision_id:
            current = self.source.reference_for(item.id, item.revision_id)
            if current != reference:
                reasons.append("Source path or physical identity changed after the job was queued.")
        elif reference is not None:
            reasons.append("Source revision changed after the job was queued.")
        return list(dict.fromkeys(reasons))

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
            if backend == "qsv":
                device = self.encoder.qsv_device
                ready = device is not None and self.encoder.qsv_device_check(device)[0]
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
            with self._lock:
                self._active.add(job.id)
            try:
                self._execute(job)
            except EncodingCancelled:
                if not self._stop.is_set() and queue:
                    queue.cancelled_real_job(job.id)
            except Exception as error:
                if queue:
                    queue.fail_real_job(job.id, str(error), getattr(error, "exit_code", None))
                self.encoder.cleanup(job.id, remove_final=True)
            finally:
                self.encoder.cleanup(job.id)
                with self._lock:
                    self._active.discard(job.id)
                    self._cancelled.discard(job.id)
                self._wake.set()

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
        result = catalog.evaluate(entry, job.preset, job.requested_preserve_audio, job.preserve_subtitles)
        if not result.eligible:
            raise Conflict("Policy changed before execution: " + "; ".join(result.reasons))

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
        if errors:
            queue.record_validation_errors(job.id, errors)
            raise Conflict("Output validation failed: " + "; ".join(errors))

        with self._lock:
            if job.id in self._cancelled or self._stop.is_set():
                raise EncodingCancelled("Encoding stopped before output promotion.")
            self.guard.before_processing(reference)
            final_path = self.encoder.promote(job.id, output)
            size = final_path.stat().st_size
            if not queue.complete_real_job(job.id, str(final_path), size):
                self.encoder.cleanup(job.id, remove_final=True)
                raise EncodingCancelled("Job was stopped before completion was persisted.")

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
