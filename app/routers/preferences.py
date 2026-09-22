from fastapi import APIRouter, HTTPException, Response
from pydantic import ValidationError

from app.dependencies import Processor
from app.models.preset import PresetSettings
from app.models.tags import TagTarget, TagUpdate


router = APIRouter(prefix="/api", tags=["Presets and tags"])


@router.post("/presets", status_code=201)
def create_preset(processor: Processor, payload: PresetSettings):
    return processor.create_preset(payload)


@router.put("/presets/{preset_id}")
def update_preset(processor: Processor, preset_id: str, payload: PresetSettings):
    return processor.update_preset(preset_id, payload)


@router.post("/presets/{preset_id}/duplicate", status_code=201)
def duplicate_preset(processor: Processor, preset_id: str):
    return processor.duplicate_preset(preset_id)


@router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(processor: Processor, preset_id: str) -> Response:
    processor.delete_preset(preset_id)
    return Response(status_code=204)


@router.get("/tags")
def get_tags(processor: Processor, kind: str, id: str, season: int | None = None):
    try:
        target = TagTarget(kind=kind, id=id, season=season)
    except ValidationError as error:
        raise HTTPException(422, "Invalid tag target.") from error
    return processor.get_tags(target)


@router.patch("/tags")
def update_tags(processor: Processor, payload: TagUpdate):
    return processor.update_tags(payload)
