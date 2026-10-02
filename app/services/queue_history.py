"""History actions on finished jobs: Replace source, comparisons, deletion."""
from uuid import uuid4

from app.services.errors import InvalidOperation
from app.services.queue_common import PENDING, QueueConflict, QueueParts, now


class QueueHistory(QueueParts):
    """Finished jobs: swapping a kept output in, quality comparisons and forgetting jobs."""

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
        reference = self._find(job_id).source_reference
        check = getattr(self.worker, "replacement_check", None)
        replacement = check(reference) if check and reference is not None else []  # Before the lock.
        with self.lock, self.repository.transaction():
            kept = self._find(job_id)
            if (kept.execution_mode != "real" or kept.status != "completed" or kept.replace_source
                    or kept.source_replaced or not kept.output_path):
                raise QueueConflict("Only completed keep-original real encodes with a kept output can replace their source.")
            if any(j.media_id == kept.media_id and j.scope == kept.scope for j in self.repository.pending()):
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
            result = self.catalog.evaluate(entry, job.preset, job.requested_preserve_audio, job.preserve_subtitles_and_metadata,
                                           job.chosen_video_bitrate)
            reasons = list(result.reasons)
            if result.preserve_audio != job.preserve_audio:
                reasons.append("The audio policy changed since this output was encoded; encode again instead.")
            capability_check = getattr(self.worker, "enqueue_reasons", None)
            if capability_check:
                reasons.extend(capability_check(entry.item, job, replacement=replacement))
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

    def delete_history(self, job_id: str) -> None:
        """Forget a finished job. A kept output still in the workspace goes with it."""
        with self.lock, self.repository.transaction():
            job = self._find(job_id)
            if job.status in PENDING:
                raise QueueConflict("Only finished jobs can be deleted from History.")
            if any(j.replaces_job_id == job.id for j in self.repository.pending()):
                raise QueueConflict("A replacement using this kept output is queued or running.")
            self.repository.remove(job_id)
        cleanup = getattr(self.worker, "cleanup_interrupted", None)
        if cleanup and job.execution_mode == "real":
            # Only Kompressor's own outputs in <workspace>/jobs/<id>; never the source.
            cleanup(job.id)
