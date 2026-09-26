"""Central adapter selection: seed simulation or read-only filesystem discovery."""
from pathlib import Path
from typing import Literal
from app.services.encoding_runtime import capability_status

from app.config import Settings
from app.models.preferences import LibraryPaths, WorkerSettings
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
from app.services.worker_control import WorkerControlService
from app.workers.fake import FakeEncoderWorker
from app.workers.encoder import FakeEncoder
from app.services.discovery import FakeMediaScanner, FakeProbeService
from app.services.media_processor import MediaProcessor
from app.services.filesystem_source import FilesystemObservationSource
from app.services.encoding_capability import CPUEncodeCapability, QSVEncodeCapability
from app.services.source_guard import SourceGuard
from app.workers.ffmpeg import FFmpegEncoder
from app.workers.real import RealEncoderWorker
from app.services.presets import PresetService


def build_media_processor(config: Settings, database_path: Path | None = None, *,
                          media: MediaRepository | None = None,
                          initial_presets: list[CompressionPreset] | None = None,
                          process_role: Literal["web", "worker"] = "web",
                          worker_backend: Literal["cpu", "qsv"] = "cpu") -> MediaProcessor:
    db_path = database_path if database_path is not None else config.database_path
    defaults = LibraryPaths(movies_path=str(config.movies_root) if config.movies_root else "",
                            shows_path=str(config.shows_root) if config.shows_root else "")
    database = Database(db_path)
    preferences = SQLitePreferencesRepository(database, defaults, WorkerSettings(timezone=config.timezone))
    configured_roots(preferences.get_library_paths(), db_path)
    inventory_repository = SQLiteInventoryRepository(database)
    reconciliation = ReconciliationService(inventory_repository)
    discovery: LibraryDiscoveryService | None = None
    media_repository: MediaRepository
    worker = None
    runtime = None
    if config.media_backend == "filesystem" and media is None:
        def roots() -> dict[str, Path]:
            return configured_roots(preferences.get_library_paths(), db_path)
        roots()
        runtime = capability_status(
            config.ffmpeg_binary, config.ffprobe_binary, config.workspace_root, roots(), config.qsv_device)
        ffprobe = FFprobeService(config.ffprobe_binary, config.ffprobe_timeout)
        source = FilesystemObservationSource(inventory_repository, roots)
        supported = frozenset(runtime.get("supported_backends") or (["cpu"] if runtime.get("available") else []))
        owned_backend = worker_backend if process_role == "worker" else None
        unavailable_reason = (
            runtime.get(f"{worker_backend}_unavailable_reason")
            if owned_backend is not None else runtime["unavailable_reason"]
        )
        worker = RealEncoderWorker(
            backend=owned_backend,
            supported_backends=supported,
            unavailable_reason=unavailable_reason,
            encoder=FFmpegEncoder(config.ffmpeg_binary, config.workspace_root, qsv_device=config.qsv_device),
            source=source,
            guard=SourceGuard(inventory_repository, source),
            capabilities={"cpu": CPUEncodeCapability(), "qsv": QSVEncodeCapability()},
            probe=ffprobe,
        )
        media_repository = FilesystemMediaRepository(
            inventory_repository, roots, encoding_enabled=bool(supported))
        discovery = LibraryDiscoveryService(FilesystemScanner(), ffprobe, reconciliation, roots, db_path)
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
    if worker is None:
        worker = FakeEncoderWorker(FakeEncoder())
    controls = WorkerControlService(preferences)
    queue = QueueService(SQLiteQueueRepository(database), catalog, worker, controls=controls)
    if isinstance(worker, RealEncoderWorker):
        worker.bind(queue, catalog)
        diagnostics = worker.diagnostics(runtime)
    else:
        diagnostics = {"ffmpeg_binary": config.ffmpeg_binary, "ffprobe_binary": config.ffprobe_binary,
                       "ffmpeg_available": None, "ffprobe_available": None, "libx265_available": None,
                       "hevc_qsv_available": None, "qsv_device": str(config.qsv_device),
                       "qsv_available": False, "cpu_available": False,
                       "workspace_root": str(config.workspace_root), "encoding_enabled": False,
                       "encoder_mode": "seed fake simulation" if discovery is None else "disabled",
                       "supported_backends": ["cpu", "qsv"] if discovery is None else [],
                       "workspace_writable": bool(runtime and runtime["workspace_writable"]),
                       "unavailable_reason": runtime["unavailable_reason"] if runtime else None}
    # The web process must never reinterpret an active real job as interrupted:
    # a standalone worker may still own its ffmpeg subprocess. Recovery therefore
    # belongs to the real worker process. Seed mode remains embedded and recovers
    # with the web application as before.
    if not isinstance(worker, RealEncoderWorker):
        queue.recover()
    elif process_role == "worker":
        queue.recover(backends={worker_backend})
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
                          "ffprobe_timeout": config.ffprobe_timeout, **diagnostics},
    )
