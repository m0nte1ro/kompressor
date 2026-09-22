from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response

from app.models.preset import CompressionPreset, PresetSettings
from app.models.tags import TAG_NAMES, TagTarget, TagUpdate


router = APIRouter(prefix="/api", tags=["Presets and tags"])


def ensure_supported(payload: PresetSettings):
    if payload.enabled and not payload.source_resolutions:
        raise HTTPException(422, "Choose at least one supported source resolution.")
    if payload.enabled and payload.destination_codec == "av1":
        raise HTTPException(422, "AV1 presets cannot be enabled yet.")
    if payload.rate_control == "icq" and payload.quality_value is not None and not 18 <= payload.quality_value <= 30:
        raise HTTPException(422, "QSV ICQ quality must be between 18 and 30 for practical presets.")


@router.post("/presets", status_code=201)
def create_preset(request: Request, payload: PresetSettings):
    ensure_supported(payload)
    preset = CompressionPreset(id=str(uuid4()), **payload.model_dump(exclude={"origin"}), origin="custom")
    request.app.state.catalog.presets.save(preset)
    return preset.model_dump()


@router.put("/presets/{preset_id}")
def update_preset(request: Request, preset_id: str, payload: PresetSettings):
    ensure_supported(payload)
    repository = request.app.state.catalog.presets
    with request.app.state.database.transaction():
        if repository.get_by_id(preset_id) is None:
            raise HTTPException(404, "Preset not found.")
        existing = repository.get_by_id(preset_id)
        preset = CompressionPreset(id=preset_id, **payload.model_dump(exclude={"origin"}), origin=existing.origin)
        repository.save(preset)
    return preset.model_dump()


@router.post("/presets/{preset_id}/duplicate", status_code=201)
def duplicate_preset(request: Request, preset_id: str):
    repository = request.app.state.catalog.presets
    with request.app.state.database.transaction():
        source = repository.get_by_id(preset_id)
        if source is None:
            raise HTTPException(404, "Preset not found.")
        preset = source.model_copy(update={"id": str(uuid4()), "name": source.name[:112] + " (Copy)", "origin": "custom"}, deep=True)
        repository.save(preset)
    return preset.model_dump()


@router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(request: Request, preset_id: str) -> Response:
    repository = request.app.state.catalog.presets
    with request.app.state.database.transaction():
        preset = repository.get_by_id(preset_id)
        if preset is None:
            raise HTTPException(404, "Preset not found.")
        if preset.origin == "built_in":
            raise HTTPException(409, "Built-in presets cannot be deleted. Disable or duplicate this preset instead.")
        repository.delete(preset_id)
    return Response(status_code=204)


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
