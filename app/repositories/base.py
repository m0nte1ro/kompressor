from __future__ import annotations

from typing import Protocol

from app.models.media import MediaLibrary


class MediaRepository(Protocol):
    def get_library(self) -> MediaLibrary:
        ...
