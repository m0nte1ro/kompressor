"""Application facade. Transport, persistence and encoder details stay outside."""
from pathlib import Path

from app.models.media import MediaScope
from app.models.inventory import ReconciliationResult, ScanSnapshot
from app.services.reconciliation import ReconciliationService
from app.models.preferences import LibraryPaths
from app.models.preset import PresetSettings
from app.models.queue import EnqueueRequest, Priority
from app.models.tags import TAG_NAMES, TagTarget, TagUpdate
from app.services.catalog import CatalogService
from app.services.errors import InvalidOperation
from app.services.presets import PresetService
from app.repositories.preferences import SQLitePreferencesRepository
from app.services.queue import QueueService
from app.services.library_discovery import LibraryDiscoveryService, configured_roots
from app.services.discovery import MediaScanner, ProbeService
from app.services.tags import TagService
from app.services.encoding_runtime import roots_overlap_workspace


class MediaProcessor:
    def __init__(self, catalog: CatalogService, presets: PresetService,
                 queue: QueueService, scanner: MediaScanner | None, probe: ProbeService | None,
                 preferences: SQLitePreferencesRepository | None = None,
                 inventory: ReconciliationService | None = None,
                 discovery: LibraryDiscoveryService | None = None, runtime_settings: dict | None = None):
        self.catalog = catalog
        self.presets = presets
        self.queue = queue
        self.scanner = scanner
        self.probe = probe
        self.preferences = preferences
        self.inventory = inventory
        self.discovery = discovery
        self.runtime_settings = runtime_settings or {}

    def get_library(self):
        return self.catalog.library()

    def get_summary(self):
        return self.catalog.summary()

    def get_movies(self):
        return self.catalog.previews("movie")

    def get_shows(self):
        return self.catalog.shows()

    def get_show(self, show_id: str):
        return self.catalog.show_detail(show_id)

    def get_presets(self, scope: MediaScope | None = None):
        return self.presets.get_all(scope)

    def get_library_paths(self) -> LibraryPaths:
        return self.preferences.get_library_paths() if self.preferences else LibraryPaths()

    def update_library_paths(self, paths: LibraryPaths) -> LibraryPaths:
        if self.preferences is None:
            raise InvalidOperation("Preferences storage is unavailable.")
        roots = configured_roots(paths, self.preferences.database.path)
        workspace = self.runtime_settings.get("workspace_root")
        if workspace and roots_overlap_workspace(Path(workspace), roots):
            raise InvalidOperation("Library roots must not overlap the Kompressor output workspace.")
        return self.preferences.save_library_paths(paths)

    def get_runtime_settings(self) -> dict:
        return {**dict(self.runtime_settings), "startup": dict(self.runtime_settings), "startup_apply_timing": "restart_required",
                "library_paths": self.get_library_paths().model_dump(),
                "paths_apply_timing": "next_scan; active scan keeps captured roots",
                "paths_precedence": "persisted settings (including empty) > environment/.env > empty defaults",
                "presets_apply_timing": "immediate eligibility; new jobs only; queued snapshots unchanged",
                "encoding_enabled": self.runtime_settings.get("encoding_enabled", False),
                "development_mode": "unused compatibility flag"}

    def create_preset(self, payload: PresetSettings):
        return self.presets.create(payload)

    def update_preset(self, preset_id: str, payload: PresetSettings):
        return self.presets.update(preset_id, payload)

    def duplicate_preset(self, preset_id: str):
        return self.presets.duplicate(preset_id)

    def delete_preset(self, preset_id: str):
        self.presets.delete(preset_id)

    def evaluate_compression(self, media_id: str, scope: MediaScope, preset_id: str,
                             preserve_audio: bool | None = None, preserve_subtitles: bool = True):
        preset = self.catalog.preset(preset_id)
        entry = self.catalog.find(media_id, scope)
        return self.catalog.evaluate(entry, preset, preserve_audio, preserve_subtitles)

    def queue_encode(self, request: EnqueueRequest):
        # QueueService and the configured worker enforce policy and runtime capability.
        return self.queue.enqueue(request)

    def get_queue(self):
        return self.queue.snapshot()

    def remove_queued_job(self, job_id: str):
        self.queue.remove(job_id)

    def prioritize_job(self, job_id: str, priority: Priority):
        self.queue.prioritize(job_id, priority)

    def move_job_next(self, job_id: str):
        self.queue.prioritize(job_id)

    def stop_job(self, job_id: str):
        self.queue.skip(job_id)

    def _tagger(self) -> TagService:
        if self.catalog.tagger is None:
            raise InvalidOperation("Tag service is unavailable.")
        return self.catalog.tagger

    def get_tags(self, target: TagTarget):
        return {"available": TAG_NAMES, **self._tagger().describe(target)}

    def update_tags(self, payload: TagUpdate):
        # Tag changes and queue revalidation commit atomically. Any real process
        # cancellation waits until both the queue lock and DB transaction release.
        with self.queue.lock, self.queue.repository.transaction():
            updated = self._tagger().update(payload)
            stop_after_commit = self.queue.revalidate(defer_external_stops=True)
        for job_id in stop_after_commit:
            self.queue.worker.stop(job_id)
        return {"updated": updated}

    def scan_library(self):
        if self.discovery is not None:
            self.discovery.scan()
            return self.catalog.library()
        if self.scanner is None:
            raise InvalidOperation("Scanner is unavailable.")
        return self.scanner.scan()

    def refresh_library(self) -> dict:
        if self.discovery is None:
            raise InvalidOperation("Real scanning requires the filesystem backend.")
        return self.discovery.start()

    def get_scan_status(self) -> dict:
        return self.discovery.status() if self.discovery else {"backend": "seed", "state": "disabled", "roots": []}

    def reconcile_library(self, snapshot: ScanSnapshot) -> ReconciliationResult:
        """Apply observations supplied by fixtures; never initiate filesystem IO."""
        if self.inventory is None:
            raise InvalidOperation("Reconciliation service is unavailable.")
        return self.inventory.reconcile(snapshot)
