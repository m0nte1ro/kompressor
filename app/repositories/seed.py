from __future__ import annotations

import json
from pathlib import Path

from app.models.media import MediaLibrary


class SeedMediaRepository:
    def __init__(self, fixture_path: Path) -> None:
        self.fixture_path = fixture_path

    def get_library(self) -> MediaLibrary:
        raw = self.fixture_path.read_text(
            encoding="utf-8",
        )

        payload = json.loads(raw)

        return MediaLibrary.model_validate(
            payload,
        )
