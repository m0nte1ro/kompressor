from app.models.preferences import LibraryPaths
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.dependencies import Processor


router = APIRouter()


class EligibilityRequest(BaseModel):
    media_id: str
    scope: Literal["movie", "show"]
    preset_id: str
    preserve_audio: bool | None = None
    preserve_subtitles: bool = True


@router.get("/healthz")
def healthz():
    return {"status": "ok", "app": settings.app_name, "media_backend": settings.media_backend}


@router.get("/api/library")
def get_library(processor: Processor):
    return processor.get_library()


@router.get("/api/summary")
def get_summary(processor: Processor):
    return processor.get_summary()


@router.get("/api/presets")
def get_presets(processor: Processor, scope: Literal["movie", "show"] | None = None):
    return processor.get_presets(scope)


@router.get("/api/settings")
def get_settings(processor: Processor):
    return processor.get_library_paths()


@router.put("/api/settings")
def update_settings(processor: Processor, payload: LibraryPaths):
    return processor.update_library_paths(payload)


@router.post("/api/eligibility")
def evaluate_eligibility(processor: Processor, payload: EligibilityRequest):
    return processor.evaluate_compression(**payload.model_dump())
