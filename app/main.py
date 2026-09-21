from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from time import monotonic
from typing import Literal

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
)
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, settings
from app.repositories.preset_seed import (
    SeedPresetRepository,
)
from app.repositories.seed import (
    SeedMediaRepository,
)
from app.services.policy import PolicyEngine
from app.services.catalog import CatalogService
from app.services.queue import QueueService
from app.repositories.queue_fake import FakeQueueRepository
from app.workers.fake import FakeEncoderWorker
from app.routers.queue import router as queue_router
from app.routers.web import router as web_router


media_repository = SeedMediaRepository(
    fixture_path=settings.seed_media_path,
)

preset_repository = SeedPresetRepository(
    fixture_path=settings.seed_presets_path,
)

policy_engine = PolicyEngine()
catalog = CatalogService(media_repository, preset_repository, policy_engine)


async def run_fake_workers(service: QueueService) -> None:
    previous = monotonic()
    while True:
        await asyncio.sleep(1)
        current = monotonic()
        service.tick(current - previous)
        previous = current


@asynccontextmanager
async def lifespan(application: FastAPI):
    task = asyncio.create_task(run_fake_workers(application.state.queue_service))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.state.catalog = catalog
app.state.queue_service = QueueService(
    FakeQueueRepository(PROJECT_ROOT / "fixtures" / "queue.json"),
    catalog, FakeEncoderWorker(),
)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
app.include_router(queue_router)
app.include_router(web_router)


class EligibilityRequest(BaseModel):
    media_id: str

    scope: Literal[
        "movie",
        "show",
    ]

    preset_id: str

    preserve_audio: bool = False
    preserve_subtitles: bool = True


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "media_backend": (
            settings.media_backend
        ),
    }


@app.get("/api/library")
def get_library() -> dict:
    library = (
        media_repository.get_library()
    )

    return library.model_dump()


@app.get("/api/summary")
def get_summary() -> dict:
    library = (
        media_repository.get_library()
    )

    movie_count = len(
        library.movies
    )

    show_count = len(
        library.shows
    )

    episode_count = sum(
        len(season.episodes)
        for show in library.shows
        for season in show.seasons
    )

    movie_bytes = sum(
        movie.size
        for movie in library.movies
    )

    show_bytes = sum(
        episode.size
        for show in library.shows
        for season in show.seasons
        for episode in season.episodes
    )

    return {
        "movies": movie_count,
        "shows": show_count,
        "episodes": episode_count,
        "movie_bytes": movie_bytes,
        "show_bytes": show_bytes,
    }


@app.get("/api/presets")
def get_presets(
    scope: Literal[
        "movie",
        "show",
    ] | None = Query(
        default=None,
    ),
) -> list[dict]:
    presets = (
        preset_repository.get_all()
    )

    if scope is not None:
        presets = [
            preset
            for preset in presets
            if preset.scope == scope
        ]

    return [
        preset.model_dump()
        for preset in presets
    ]


@app.post("/api/eligibility")
def evaluate_eligibility(
    request: EligibilityRequest,
) -> dict:
    try:
        preset = catalog.preset(request.preset_id)
        entry = catalog.find(request.media_id, request.scope)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return catalog.evaluate(
        entry, preset, request.preserve_audio, request.preserve_subtitles,
    ).model_dump()
