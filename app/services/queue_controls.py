"""Worker pause, quiet hours and lane stops."""
from app.models.preferences import WorkerSettings
from app.services.errors import InvalidOperation
from app.services.queue_common import ACTIVE, QueueParts


class WorkerControls(QueueParts):
    """Pausing lanes, quiet hours and stopping a lane's active job."""

    def _worker_snapshot(self) -> dict | None:
        if self.controls is None:
            return None
        snapshot = self.controls.snapshot()
        for backend, lane in snapshot["lanes"].items():
            available = backend in self.supported_backends
            lane["available"] = available
            lane["accepting_jobs"] = bool(lane["accepting_jobs"] and available)
        return snapshot

    def quiet_active(self, backend: str) -> bool:
        return bool(self.controls and self.controls.quiet_active(backend))

    def enforce_quiet_start(self, backend: str) -> str | None:
        if self.controls is None or not self.controls.quiet_active(backend):
            return None
        with self.lock, self.repository.transaction():
            active = next((j for j in self.repository.pending()
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
            active = next((j for j in self.repository.pending()
                           if j.backend == backend and j.status in ACTIVE), None)
            job_id = active.id if active and active.status != "replacing" else None
        if job_id is None:
            return False
        self.skip(job_id, reason=reason)
        return True
