from __future__ import annotations

from typing import Literal

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
)
from pydantic import BaseModel

from app.config import settings
from app.models.media import (
    Episode,
    Movie,
)
from app.repositories.preset_seed import (
    SeedPresetRepository,
)
from app.repositories.seed import (
    SeedMediaRepository,
)
from app.services.policy import PolicyEngine


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
)


media_repository = SeedMediaRepository(
    fixture_path=settings.seed_media_path,
)

preset_repository = SeedPresetRepository(
    fixture_path=settings.seed_presets_path,
)

policy_engine = PolicyEngine()


class EligibilityRequest(BaseModel):
    media_id: str

    scope: Literal[
        "movie",
        "show",
    ]

    preset_id: str

    preserve_audio: bool = False
    preserve_subtitles: bool = True


def find_movie(
    media_id: str,
) -> Movie | None:
    library = media_repository.get_library()

    for movie in library.movies:
        if movie.id == media_id:
            return movie

    return None


def find_episode(
    media_id: str,
) -> tuple[
    Episode,
    list[str],
] | None:
    library = media_repository.get_library()

    for show in library.shows:
        for season in show.seasons:
            for episode in season.episodes:
                if episode.id != media_id:
                    continue

                effective_tags = list(
                    dict.fromkeys(
                        [
                            *show.tags,
                            *season.tags,
                            *episode.tags,
                        ]
                    )
                )

                return (
                    episode,
                    effective_tags,
                )

    return None


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
    preset = (
        preset_repository.get_by_id(
            request.preset_id,
        )
    )

    if preset is None:
        raise HTTPException(
            status_code=404,
            detail="Preset not found.",
        )

    if request.scope == "movie":
        movie = find_movie(
            request.media_id,
        )

        if movie is None:
            raise HTTPException(
                status_code=404,
                detail="Movie not found.",
            )

        result = policy_engine.evaluate(
            item=movie,
            scope="movie",
            preset=preset,
            effective_tags=movie.tags,
            preserve_audio=(
                request.preserve_audio
            ),
            preserve_subtitles=(
                request.preserve_subtitles
            ),
        )

        return result.model_dump()

    episode_result = find_episode(
        request.media_id,
    )

    if episode_result is None:
        raise HTTPException(
            status_code=404,
            detail="Episode not found.",
        )

    (
        episode,
        effective_tags,
    ) = episode_result

    result = policy_engine.evaluate(
        item=episode,
        scope="show",
        preset=preset,
        effective_tags=effective_tags,
        preserve_audio=(
            request.preserve_audio
        ),
        preserve_subtitles=(
            request.preserve_subtitles
        ),
    )

    return result.model_dump()
