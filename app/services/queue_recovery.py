"""Startup recovery of the lanes a process owns."""
from pathlib import Path

from app.models.queue import QueueJob
from app.services.queue_common import ACTIVE, QueueParts, now


class QueueRecovery(QueueParts):
    """Interrupted swaps are resolved first; other interrupted jobs are requeued from zero."""

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
        for job in self.repository.pending():
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
                    current.move_to("completed")
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
                    current.move_to("completed")
                    current.replace_source = False
                    current.output_size = Path(current.output_path).stat().st_size
                    current.measured_saving = current.source_size - current.output_size
                    current.reasons = [*current.reasons, "Worker stopped during source replacement. " + detail
                                       + " The validated output was kept; use Replace source in History."]
                    keep_outputs.add(current.id)
                else:
                    current.move_to("failed")
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
        # Covers the active jobs requeued below: a worker restarting before the
        # library is mounted must leave them waiting, not block them.
        storage = self.storage_checks(backends)
        with self.lock, self.repository.transaction():
            for job in self.repository.get_all():
                if backends is not None and job.backend not in backends:
                    continue
                if job.status == "replacing":
                    continue  # Only reachable without a replacement-aware worker; never requeue a swap.
                if job.status == "stopping" and job.cancel_requested:
                    if job.backend in self.external_backends:
                        interrupted.append(job.id)
                    job.move_to("skipped")
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
                        job.move_to("blocked")
                        job.reasons = [f"This {job.execution_mode} job cannot resume in {self.execution_mode} mode."]
                        job.finished_at = now()
                    else:
                        # Runtime lane availability is transient (for example a
                        # render device can disappear across a host/LXC restart).
                        # An owned interrupted real job is requeued and waits for
                        # its worker instead of becoming permanently blocked.
                        if job.backend in self.external_backends:
                            interrupted.append(job.id)
                        job.move_to("queued")
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
                    job.move_to("blocked")
                    job.reasons = [f"This {job.execution_mode} job cannot run in {self.execution_mode} mode."]
                    job.finished_at = now()
                    self.repository.save(job)
                elif job.status == "blocked" and job.cancel_requested:
                    # A prior worker may have died after policy revalidation made
                    # the job terminal but before acknowledging the stop request.
                    job.cancel_requested = False
                    self.repository.save(job)
            if backends is None:
                self.revalidate(storage=storage)
            else:
                ready_backends = frozenset(backends) & self.supported_backends
                if ready_backends:
                    self.revalidate(backends=ready_backends, storage=storage)
        cleanup = getattr(self.worker, "cleanup_interrupted", None)
        if cleanup:
            for job_id in interrupted:
                cleanup(job_id)
