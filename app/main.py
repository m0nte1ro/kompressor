from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from time import monotonic

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, settings
from app.models.preset import CompressionPreset
from app.repositories.base import MediaRepository
from app.repositories.database import Database
from app.repositories.preset_seed import SeedPresetRepository
from app.repositories.seed import SeedMediaRepository
from app.repositories.sqlite import SQLitePresetRepository, SQLiteQueueRepository, SQLiteTagRepository
from app.routers.api import router as api_router
from app.routers.preferences import router as preferences_router
from app.routers.queue import router as queue_router
from app.routers.web import router as web_router
from app.services.catalog import CatalogService
from app.services.policy import PolicyEngine
from app.services.queue import QueueService
from app.services.tags import TagService
from app.workers.fake import FakeEncoderWorker


async def run_fake_workers(service: QueueService) -> None:
    previous = monotonic()
    while True:
        await asyncio.sleep(1)
        current = monotonic()
        service.tick(current - previous)
        previous = current


def create_app(database_path: Path | None = None, *, media: MediaRepository | None = None,
               start_workers: bool = True, initial_presets: list[CompressionPreset] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = Database(database_path if database_path is not None else settings.database_path)
        media_repository = media if media is not None else SeedMediaRepository(settings.seed_media_path)
        presets = SQLitePresetRepository(database, initial_presets if initial_presets is not None else SeedPresetRepository(settings.seed_presets_path).get_all())
        if initial_presets is None:
            from app.repositories.preset_migration import upgrade_streaming_presets
            upgrade_streaming_presets(database, SeedPresetRepository(settings.seed_presets_path).get_all())
        tagger = TagService(media_repository, SQLiteTagRepository(database))
        catalog = CatalogService(media_repository, presets, PolicyEngine(), tagger)
        queue = QueueService(SQLiteQueueRepository(database), catalog, FakeEncoderWorker())
        queue.recover()
        application.state.database = database
        application.state.catalog = catalog
        application.state.queue_service = queue
        task = asyncio.create_task(run_fake_workers(queue)) if start_workers else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    application = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
    for router in (api_router, preferences_router, queue_router, web_router):
        application.include_router(router)
    return application


# Database and seed IO happen at startup, never as an import side effect.
app = create_app()
