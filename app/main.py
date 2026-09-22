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
from app.container import build_media_processor
from app.dependencies import register_error_handlers
from app.services.media_processor import MediaProcessor
from app.routers.api import router as api_router
from app.routers.preferences import router as preferences_router
from app.routers.queue import router as queue_router
from app.routers.web import router as web_router
from app.services.queue import QueueService


async def run_fake_workers(service: QueueService) -> None:
    previous = monotonic()
    while True:
        await asyncio.sleep(1)
        current = monotonic()
        service.tick(current - previous)
        previous = current


def create_app(database_path: Path | None = None, *, media: MediaRepository | None = None,
               start_workers: bool = True, initial_presets: list[CompressionPreset] | None = None,
               processor: MediaProcessor | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        composed = processor if processor is not None else build_media_processor(
            settings, database_path, media=media, initial_presets=initial_presets)
        application.state.media_processor = composed
        queue = composed.queue
        task = asyncio.create_task(run_fake_workers(queue)) if start_workers else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    application = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)
    register_error_handlers(application)
    application.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
    for router in (api_router, preferences_router, queue_router, web_router):
        application.include_router(router)
    return application


# Database and seed IO happen at startup, never as an import side effect.
app = create_app()
