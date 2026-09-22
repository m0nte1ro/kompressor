from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models.media import MediaLibrary
from app.repositories.seed import SeedMediaRepository


ROOT = Path(__file__).resolve().parents[1]


class MemoryMedia:
    def __init__(self, library: MediaLibrary):
        self.library = library

    def get_library(self):
        return self.library


@pytest.fixture
def runtime(tmp_path):
    media = MemoryMedia(SeedMediaRepository(ROOT / "fixtures/media.json").get_library())
    application = main.create_app(tmp_path / "state.sqlite3", media=media, start_workers=False)
    with TestClient(application) as client:
        yield application, client


@pytest.fixture
def catalog(runtime):
    return runtime[0].state.catalog


@pytest.fixture
def queue(runtime):
    return runtime[0].state.queue_service


@pytest.fixture
def client(runtime):
    return runtime[1]


@pytest.fixture
def movie_payload():
    return {"scope": "movie", "preset_id": "movie-1080p-quality", "media_ids": ["movie-king-of-comedy"]}


@pytest.fixture
def show_payload():
    return {"scope": "show", "preset_id": "show-1080p", "media_ids": ["modern-family-s03e04"]}
