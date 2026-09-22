"""Preset management and transactional catalogue edits."""
from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import uuid4

from app.models.preset import CompressionPreset, PresetSettings
from app.repositories.base import PresetRepository
from app.services.errors import Conflict, InvalidOperation, NotFound


def ensure_supported(payload: PresetSettings):
    if payload.enabled and not payload.source_resolutions:
        raise InvalidOperation("Choose at least one supported source resolution.")
    if payload.enabled and payload.destination_codec == "av1":
        raise InvalidOperation("AV1 presets cannot be enabled yet.")
    if payload.rate_control == "icq" and payload.quality_value is not None and not 18 <= payload.quality_value <= 30:
        raise InvalidOperation("QSV ICQ quality must be between 18 and 30 for practical presets.")


class PresetService:
    def __init__(self, repository: PresetRepository,
                 transaction: Callable[[], AbstractContextManager]):
        self.repository = repository
        self.transaction = transaction

    def get_all(self, scope: str | None = None):
        return [p for p in self.repository.get_all() if scope is None or p.scope == scope]

    def _get(self, preset_id: str):
        preset = self.repository.get_by_id(preset_id)
        if preset is None:
            raise NotFound("Preset not found.")
        return preset

    def create(self, payload: PresetSettings):
        ensure_supported(payload)
        preset = CompressionPreset(id=str(uuid4()), **payload.model_dump(exclude={"origin"}), origin="custom")
        self.repository.save(preset)
        return preset

    def update(self, preset_id: str, payload: PresetSettings):
        ensure_supported(payload)
        with self.transaction():
            existing = self._get(preset_id)
            preset = CompressionPreset.model_validate({
                **payload.model_dump(exclude={"origin"}),
                "id": preset_id,
                "origin": existing.origin,
            })
            self.repository.save(preset)
        return preset

    def duplicate(self, preset_id: str):
        with self.transaction():
            source = self._get(preset_id)
            preset = source.model_copy(update={"id": str(uuid4()), "name": source.name[:112] + " (Copy)", "origin": "custom"}, deep=True)
            self.repository.save(preset)
        return preset

    def delete(self, preset_id: str):
        with self.transaction():
            preset = self._get(preset_id)
            if preset.origin == "built_in":
                raise Conflict("Built-in presets cannot be deleted. Disable or duplicate this preset instead.")
            self.repository.delete(preset_id)
