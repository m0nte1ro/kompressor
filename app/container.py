"""The only composition root: seed discovery, SQLite state and fake execution."""
from pathlib import Path

from app.config import Settings
from app.models.preset import CompressionPreset
from app.repositories.base import MediaRepository
from app.repositories.database import Database
from app.repositories.inventory import SQLiteInventoryRepository
from app.services.reconciliation import ReconciliationService
from app.repositories.preset_seed import SeedPresetRepository
from app.repositories.preferences import SQLitePreferencesRepository
from app.repositories.seed import SeedMediaRepository
from app.repositories.sqlite import SQLitePresetRepository, SQLiteQueueRepository, SQLiteTagRepository
from app.services.catalog import CatalogService
from app.services.policy import PolicyEngine
from app.services.queue import QueueService
from app.services.tags import TagService
from app.workers.fake import FakeEncoderWorker
from app.workers.encoder import FakeEncoder
from app.services.discovery import FakeMediaScanner, FakeProbeService
from app.services.media_processor import MediaProcessor
from app.services.presets import PresetService


def build_media_processor(config: Settings, database_path: Path | None = None, *,
                          media: MediaRepository | None = None,
                          initial_presets: list[CompressionPreset] | None = None) -> MediaProcessor:
    if media is None and config.media_backend != "seed":
        raise ValueError("Only the seed media backend is implemented.")
    database = Database(database_path if database_path is not None else config.database_path)
    media_repository = media if media is not None else SeedMediaRepository(config.seed_media_path)
    presets = SQLitePresetRepository(
        database,
        initial_presets if initial_presets is not None else SeedPresetRepository(config.seed_presets_path).get_all(),
    )
    preferences = SQLitePreferencesRepository(database)
    if initial_presets is None:
        from app.repositories.preset_migration import upgrade_streaming_presets
        upgrade_streaming_presets(database, SeedPresetRepository(config.seed_presets_path).get_all())
    tagger = TagService(media_repository, SQLiteTagRepository(database))
    catalog = CatalogService(media_repository, presets, PolicyEngine(), tagger)
    queue = QueueService(SQLiteQueueRepository(database), catalog, FakeEncoderWorker(FakeEncoder()))
    queue.recover()
    return MediaProcessor(
        catalog,
        PresetService(presets, database.transaction),
        queue,
        FakeMediaScanner(media_repository),
        FakeProbeService(media_repository),
        preferences=preferences,
        inventory=ReconciliationService(SQLiteInventoryRepository(database)),
    )
