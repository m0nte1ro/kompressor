from __future__ import annotations

import json
from pathlib import Path

from app.models.preset import CompressionPreset


class SeedPresetRepository:
    def __init__(
        self,
        fixture_path: Path,
    ) -> None:
        self.fixture_path = fixture_path

    def get_all(
        self,
    ) -> list[CompressionPreset]:
        raw = self.fixture_path.read_text(
            encoding="utf-8",
        )

        payload = json.loads(
            raw,
        )

        return [
            CompressionPreset.model_validate(
                item,
            )
            for item in payload["presets"]
        ]

    def get_by_id(
        self,
        preset_id: str,
    ) -> CompressionPreset | None:
        for preset in self.get_all():
            if preset.id == preset_id:
                return preset

        return None
