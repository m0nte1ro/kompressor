"""Central adapter selection: seed simulation or read-only filesystem discovery."""
from pathlib import Path

from app.config import Settings
from app.models.preferences import LibraryPaths
from app.repositories.filesystem_media import FilesystemMediaRepository
from app.services.filesystem_scanner import FilesystemScanner
from app.services.ffprobe import FFprobeService
from app.services.library_discovery import LibraryDiscoveryService, configured_roots
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
    db_path = database_path if database_path is not None else config.database_path
    defaults = LibraryPaths(movies_path=str(config.movies_root) if config.movies_root else "",
                            shows_path=str(config.shows_root) if config.shows_root else "")
    database = Database(db_path)
    preferences = SQLitePreferencesRepository(database, defaults)
    configured_roots(preferences.get_library_paths(), db_path)
    inventory_repository = SQLiteInventoryRepository(database)
    reconciliation = ReconciliationService(inventory_repository)
    discovery: LibraryDiscoveryService | None = None
    media_repository: MediaRepository
    if config.media_backend == "filesystem" and media is None:
        def roots() -> dict[str, Path]:
            return configured_roots(preferences.get_library_paths(), db_path)
        roots()
        media_repository = FilesystemMediaRepository(inventory_repository, roots)
        discovery = LibraryDiscoveryService(FilesystemScanner(),
            FFprobeService(config.ffprobe_binary, config.ffprobe_timeout), reconciliation, roots, db_path)
    else:
        media_repository = media if media is not None else SeedMediaRepository(config.seed_media_path)
    presets = SQLitePresetRepository(
        database,
        initial_presets if initial_presets is not None else SeedPresetRepository(config.seed_presets_path).get_all(),
    )
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
        FakeMediaScanner(media_repository) if discovery is None else None,
        FakeProbeService(media_repository) if discovery is None else None,
        preferences=preferences,
        inventory=reconciliation,
        discovery=discovery,
        runtime_settings={"media_backend": "filesystem" if discovery else "seed", "database_path": str(db_path),
                          "ffprobe_binary": config.ffprobe_binary, "ffprobe_timeout": config.ffprobe_timeout},
    )
