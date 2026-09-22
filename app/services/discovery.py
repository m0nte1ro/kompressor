"""Separate discovery and probing boundaries; seed adapters never inspect /media."""
from typing import Protocol, runtime_checkable

from app.models.media import Episode, MediaLibrary, Movie
from app.repositories.base import MediaRepository
from app.services.errors import NotFound


@runtime_checkable
class MediaScanner(Protocol):
    def scan(self) -> MediaLibrary: ...


@runtime_checkable
class ProbeService(Protocol):
    def inspect(self, path: str) -> Movie | Episode: ...


class FakeMediaScanner:
    def __init__(self, media: MediaRepository):
        self.media = media

    def scan(self) -> MediaLibrary:
        return self.media.get_library().model_copy(deep=True)


class FakeProbeService:
    def __init__(self, media: MediaRepository):
        self.media = media

    def inspect(self, path: str) -> Movie | Episode:
        library = self.media.get_library()
        items = [*library.movies, *(e for s in library.shows for season in s.seasons for e in season.episodes)]
        for item in items:
            if item.path == path:
                return item.model_copy(deep=True)
        raise NotFound("Media path not found in seed inventory.")
