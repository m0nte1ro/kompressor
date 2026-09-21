from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models.media import MediaLibrary
from app.repositories.preset_seed import SeedPresetRepository
from app.repositories.queue_fake import FakeQueueRepository
from app.repositories.seed import SeedMediaRepository
from app.services.catalog import CatalogService
from app.services.policy import PolicyEngine
from app.services.queue import QueueService
from app.workers.fake import FakeEncoderWorker


ROOT = Path(__file__).resolve().parents[1]


class MemoryMedia:
    def __init__(self, library: MediaLibrary):
        self.library = library

    def get_library(self):
        return self.library


@pytest.fixture
def catalog():
    media = MemoryMedia(SeedMediaRepository(ROOT / "fixtures/media.json").get_library())
    return CatalogService(media, SeedPresetRepository(ROOT / "fixtures/presets.json"), PolicyEngine())


@pytest.fixture
def queue(catalog):
    return QueueService(FakeQueueRepository(ROOT / "fixtures/queue.json"), catalog, FakeEncoderWorker())


@pytest.fixture
def client(monkeypatch, catalog, queue):
    monkeypatch.setattr(main, "catalog", catalog)
    monkeypatch.setattr(main, "media_repository", catalog.media)
    monkeypatch.setattr(main, "preset_repository", catalog.presets)
    monkeypatch.setattr(main.app.state, "catalog", catalog)
    monkeypatch.setattr(main.app.state, "queue_service", queue)
    # No lifespan: tests control scheduler time explicitly, without wall-clock sleeps.
    test_client = TestClient(main.app)
    try:
        yield test_client
    finally:
        test_client.close()


@pytest.fixture
def movie_payload():
    return {"scope": "movie", "preset_id": "movie-1080p-quality", "media_ids": ["movie-king-of-comedy"]}


@pytest.fixture
def show_payload():
    return {"scope": "show", "preset_id": "show-1080p", "media_ids": ["modern-family-s03e04"]}
