from __future__ import annotations

from fastapi import FastAPI

from app.config import settings
from app.repositories.seed import SeedMediaRepository


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
)


repository = SeedMediaRepository(
    fixture_path=settings.seed_media_path,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "media_backend": settings.media_backend,
    }


@app.get("/api/library")
def get_library() -> dict:
    library = repository.get_library()

    return library.model_dump()


@app.get("/api/summary")
def get_summary() -> dict:
    library = repository.get_library()

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
