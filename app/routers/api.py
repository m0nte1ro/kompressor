from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import settings


router = APIRouter()


class EligibilityRequest(BaseModel):
    media_id: str
    scope: Literal["movie", "show"]
    preset_id: str
    preserve_audio: bool = False
    preserve_subtitles: bool = True


@router.get("/healthz")
def healthz():
    return {"status": "ok", "app": settings.app_name, "media_backend": settings.media_backend}


@router.get("/api/library")
def get_library(request: Request):
    return request.app.state.catalog.library().model_dump()


@router.get("/api/summary")
def get_summary(request: Request):
    library = request.app.state.catalog.library()
    episodes = [e for show in library.shows for season in show.seasons for e in season.episodes]
    return {"movies": len(library.movies), "shows": len(library.shows), "episodes": len(episodes),
            "movie_bytes": sum(m.size for m in library.movies), "show_bytes": sum(e.size for e in episodes)}


@router.get("/api/presets")
def get_presets(request: Request, scope: Literal["movie", "show"] | None = None):
    return [p.model_dump() for p in request.app.state.catalog.presets.get_all() if scope is None or p.scope == scope]


@router.post("/api/eligibility")
def evaluate_eligibility(request: Request, payload: EligibilityRequest):
    catalog = request.app.state.catalog
    try:
        preset = catalog.preset(payload.preset_id)
        entry = catalog.find(payload.media_id, payload.scope)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return catalog.evaluate(entry, preset, payload.preserve_audio, payload.preserve_subtitles).model_dump()
