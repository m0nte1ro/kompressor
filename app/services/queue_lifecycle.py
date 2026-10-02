"""How a real lane's job moves on: the state changes its standalone worker reports."""
from app.models.queue import ReplacementJournal
from app.services.queue_common import ACTIVE, QueueConflict, QueueParts, now


class RealJobLifecycle(QueueParts):
    """Progress, validation, replacement and the job's end, as the owning worker reports them."""

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
            job.move_to("validating")
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
            job.move_to("replacing")
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
            job.move_to("completed")
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
            job.move_to("skipped")
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
            job.move_to("completed")
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
            job.move_to("queued")
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
            job.move_to("failed")
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
                job.move_to("skipped")
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
