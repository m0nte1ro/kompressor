"""Persistent worker pause and quiet-hours policy shared by web and worker processes."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.preferences import WorkerSettings
from app.models.queue import QueueJob
from app.repositories.preferences import SQLitePreferencesRepository


class WorkerControlService:
    def __init__(self, repository: SQLitePreferencesRepository):
        self.repository = repository

    def get(self) -> WorkerSettings:
        return self.repository.get_worker_settings()

    def save(self, settings: WorkerSettings) -> WorkerSettings:
        return self.repository.save_worker_settings(settings)

    def set_paused(self, backend: str, paused: bool) -> WorkerSettings:
        settings = self.get()
        lane = getattr(settings, backend).model_copy(update={"paused": paused})
        return self.save(settings.model_copy(update={backend: lane}))

    def set_all_paused(self, paused: bool) -> WorkerSettings:
        settings = self.get()
        return self.save(settings.model_copy(update={
            "cpu": settings.cpu.model_copy(update={"paused": paused}),
            "qsv": settings.qsv.model_copy(update={"paused": paused}),
        }))

    def pause_all(self) -> WorkerSettings:
        return self.set_all_paused(True)

    def resume_all(self) -> WorkerSettings:
        return self.set_all_paused(False)

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = (int(part) for part in value.split(":"))
        return hour * 60 + minute

    def quiet_active(self, backend: str, at: datetime | None = None) -> bool:
        settings = self.get()
        lane = getattr(settings, backend)
        if not lane.quiet_hours_enabled:
            return False
        zone = ZoneInfo(settings.timezone)
        local = datetime.now(zone) if at is None else at.astimezone(zone)
        current = local.hour * 60 + local.minute
        start = self._minutes(lane.quiet_start)
        end = self._minutes(lane.quiet_end)
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def can_claim(self, backend: str, at: datetime | None = None) -> bool:
        settings = self.get()
        return not getattr(settings, backend).paused and not self.quiet_active(backend, at)

    def quiet_action(self, backend: str, job: QueueJob) -> str:
        lane = getattr(self.get(), backend)
        if not job.progress_known:
            return "finish"
        return "finish" if job.progress > lane.quiet_cutoff_percent else "stop"

    def snapshot(self, at: datetime | None = None) -> dict:
        settings = self.get()
        lanes = {}
        for backend in ("cpu", "qsv"):
            lane = getattr(settings, backend)
            quiet = self.quiet_active(backend, at)
            lanes[backend] = {
                **lane.model_dump(),
                "quiet_active": quiet,
                "accepting_jobs": not lane.paused and not quiet,
            }
        return {"timezone": settings.timezone, "lanes": lanes}
