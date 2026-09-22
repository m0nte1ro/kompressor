from __future__ import annotations

from typing import Protocol

from app.models.media import MediaLibrary
from app.models.preset import CompressionPreset
from app.models.queue import QueueJob


class MediaRepository(Protocol):
    def get_library(self) -> MediaLibrary:
        ...


class PresetRepository(Protocol):
    def get_all(self) -> list[CompressionPreset]: ...

    def get_by_id(self, preset_id: str) -> CompressionPreset | None: ...

    def delete(self, preset_id: str) -> None: ...


class QueueRepository(Protocol):
    def get_all(self) -> list[QueueJob]: ...

    def add(self, job: QueueJob) -> None: ...

    def remove(self, job_id: str) -> None: ...

    def save(self, job: QueueJob) -> None: ...

    def transaction(self): ...
