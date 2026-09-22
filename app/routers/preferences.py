from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from app.models.preset import CompressionPreset, PresetSettings
from app.models.tags import TAG_NAMES, TagTarget, TagUpdate


router = APIRouter(prefix="/api", tags=["Presets and tags"])


def ensure_supported(payload: PresetSettings):
    if payload.enabled and payload.destination_codec == "av1":
        raise HTTPException(422, "AV1 presets cannot be enabled yet.")


@router.post("/presets", status_code=201)
def create_preset(request: Request, payload: PresetSettings):
    ensure_supported(payload)
    preset = CompressionPreset(id=str(uuid4()), **payload.model_dump())
    request.app.state.catalog.presets.save(preset)
    return preset.model_dump()


@router.put("/presets/{preset_id}")
def update_preset(request: Request, preset_id: str, payload: PresetSettings):
    ensure_supported(payload)
    repository = request.app.state.catalog.presets
    with request.app.state.database.transaction():
        if repository.get_by_id(preset_id) is None:
            raise HTTPException(404, "Preset not found.")
        preset = CompressionPreset(id=preset_id, **payload.model_dump())
        repository.save(preset)
    return preset.model_dump()


@router.post("/presets/{preset_id}/duplicate", status_code=201)
def duplicate_preset(request: Request, preset_id: str):
    repository = request.app.state.catalog.presets
    with request.app.state.database.transaction():
        source = repository.get_by_id(preset_id)
        if source is None:
            raise HTTPException(404, "Preset not found.")
        preset = source.model_copy(update={"id": str(uuid4()), "name": source.name[:113] + " (copy)"})
        repository.save(preset)
    return preset.model_dump()


@router.get("/tags")
def get_tags(request: Request, kind: str, id: str, season: int | None = None):
    from pydantic import ValidationError
    try:
        target = TagTarget(kind=kind, id=id, season=season)
        return {"available": TAG_NAMES, **request.app.state.catalog.tagger.describe(target)}
    except ValidationError as error:
        raise HTTPException(422, "Invalid tag target.") from error
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.patch("/tags")
def update_tags(request: Request, payload: TagUpdate):
    service = request.app.state.queue_service
    # A tag change and pending-job revalidation commit together, excluding worker ticks.
    with service.lock, request.app.state.database.transaction():
        try:
            updated = request.app.state.catalog.tagger.update(payload)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        service.revalidate()
    return {"updated": updated}
