"""Application facade. Transport, persistence and encoder details stay outside."""
from app.models.media import MediaScope
from app.models.preset import PresetSettings
from app.models.queue import EnqueueRequest, Priority
from app.models.tags import TAG_NAMES, TagTarget, TagUpdate
from app.services.catalog import CatalogService
from app.services.errors import InvalidOperation
from app.services.presets import PresetService
from app.services.queue import QueueService
from app.services.discovery import MediaScanner, ProbeService
from app.services.tags import TagService


class MediaProcessor:
    def __init__(self, catalog: CatalogService, presets: PresetService,
                 queue: QueueService, scanner: MediaScanner, probe: ProbeService):
        self.catalog = catalog
        self.presets = presets
        self.queue = queue
        self.scanner = scanner
        self.probe = probe

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
        # QueueService already evaluates through CatalogService/PolicyEngine,
        # snapshots presets and excludes blocked items atomically. Do not repeat it.
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
        # Preserve atomic tag edits + revalidation, with the scheduler excluded.
        with self.queue.lock, self.queue.repository.transaction():
            updated = self._tagger().update(payload)
            self.queue.revalidate()
        return {"updated": updated}

    def scan_library(self):
        # Seed discovery only; no new HTTP scan workflow or filesystem side effects.
        return self.scanner.scan()
