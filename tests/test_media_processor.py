from contextlib import nullcontext
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.dependencies import get_media_processor
from app.main import create_app
from app.models.media import Movie, spatial_resolution
from app.models.preset import PresetSettings
from app.models.queue import EnqueueRequest, QueueJob
from app.repositories.preset_seed import SeedPresetRepository
from app.repositories.queue_fake import FakeQueueRepository
from app.repositories.seed import SeedMediaRepository
from app.services.catalog import CatalogService
from app.services.discovery import FakeMediaScanner, FakeProbeService, MediaScanner, ProbeService
from app.services.errors import Conflict, InvalidOperation, NotFound
from app.services.media_processor import MediaProcessor
from app.services.policy import PolicyEngine
from app.services.presets import PresetService
from app.services.queue import QueueService
from app.services.tags import TagService
from app.workers.encoder import Encoder, FakeEncoder
from app.workers.fake import FakeEncoderWorker


class MemoryPresets:
    def __init__(self):
        self.items = {p.id: p for p in SeedPresetRepository(PROJECT_ROOT / 'fixtures/presets.json').get_all()}

    def get_all(self):
        return list(self.items.values())

    def get_by_id(self, preset_id):
        return self.items.get(preset_id)

    def save(self, preset):
        self.items[preset.id] = preset

    def delete(self, preset_id):
        del self.items[preset_id]


class MemoryTags:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def save(self, key, assignment):
        self.items[key] = assignment

    def transaction(self):
        return nullcontext()


def seed_processor():
    media = SeedMediaRepository(PROJECT_ROOT / 'fixtures/media.json')
    presets = MemoryPresets()
    catalog = CatalogService(media, presets, PolicyEngine(), TagService(media, MemoryTags()))
    queue = QueueService(FakeQueueRepository(PROJECT_ROOT / 'fixtures/queue.json'),
                         catalog, FakeEncoderWorker(FakeEncoder()))
    return MediaProcessor(catalog, PresetService(presets, nullcontext), queue,
                          FakeMediaScanner(media), FakeProbeService(media))


def test_one_processor_per_app_and_separate_apps(tmp_path):
    first = create_app(tmp_path / 'one.sqlite3', start_workers=False)
    second = create_app(tmp_path / 'two.sqlite3', start_workers=False)
    assert not hasattr(first.state, 'media_processor')
    with TestClient(first) as a, TestClient(second) as b:
        processor = first.state.media_processor
        assert isinstance(processor, MediaProcessor)
        assert processor is not second.state.media_processor
        for _ in range(2):
            assert a.get('/api/summary').status_code == 200
            assert a.get('/movies').status_code == 200
            assert first.state.media_processor is processor
        assert b.get('/api/queue').json()['pending_count'] == 0


def test_entire_app_with_seed_and_memory_dependencies(tmp_path, monkeypatch, movie_payload):
    # No database or media tooling is needed by the facade itself.
    monkeypatch.setenv('PATH', str(tmp_path / 'no-binaries'))
    processor = seed_processor()
    app = create_app(tmp_path / 'unused.sqlite3', processor=processor, start_workers=False)
    with TestClient(app) as client:
        assert app.state.media_processor is processor
        assert client.get('/api/library').status_code == 200
        assert client.post('/api/queue', json=movie_payload).json()['added']
        assert client.get('/movies').status_code == 200
        assert client.patch('/api/tags', json={'targets': [{'kind': 'movie', 'id': 'movie-king-of-comedy'}], 'tags': ['Preserve Video']}).status_code == 200
        assert client.get('/api/queue').json()['history'][0]['status'] == 'blocked'
    assert not (tmp_path / 'unused.sqlite3').exists()


def test_routes_use_replaceable_dependency(runtime, movie_payload):
    app, client = runtime
    replacement = Mock(spec=MediaProcessor)
    replacement.get_summary.return_value = {'from': 'replacement'}
    replacement.get_queue.return_value = {'lanes': [], 'history': [], 'pending_count': 0}
    replacement.queue_encode.return_value = {'added': [], 'excluded': []}
    replacement.get_presets.return_value = []
    replacement.get_movies.return_value = []
    replacement.get_shows.return_value = []
    replacement.get_tags.return_value = {'available': []}
    replacement.update_tags.return_value = {'updated': []}
    app.dependency_overrides[get_media_processor] = lambda: replacement
    assert client.get('/api/summary').json() == {'from': 'replacement'}
    assert client.get('/api/queue').json()['pending_count'] == 0
    assert client.post('/api/queue', json=movie_payload).status_code == 201
    replacement.queue_encode.assert_called_once_with(EnqueueRequest(**movie_payload))
    assert client.get('/api/presets?scope=movie').json() == []
    replacement.get_presets.assert_called_with('movie')
    assert client.get('/movies').status_code == 200
    replacement.get_movies.assert_called_once()
    assert client.get('/shows').status_code == 200
    replacement.get_shows.assert_called_once()
    assert client.get('/settings').status_code == 200
    assert client.get('/api/tags?kind=movie&id=test').json() == {'available': []}
    assert client.patch('/api/tags', json={'targets': [{'kind': 'movie', 'id': 'test'}], 'tags': []}).json() == {'updated': []}
    replacement.get_tags.assert_called_once()
    replacement.update_tags.assert_called_once()


def test_enqueue_delegates_policy_and_never_executes_encoder(movie_payload):
    processor = seed_processor()
    encoder = Mock(wraps=FakeEncoder())
    fake_worker = cast(FakeEncoderWorker, processor.queue.worker)
    fake_worker.encoder = encoder
    policy = Mock(wraps=processor.catalog.policy)
    processor.catalog.policy = policy
    request = EnqueueRequest(**{**movie_payload, 'media_ids': ['movie-king-of-comedy', 'movie-dune-part-two']})
    result = processor.queue_encode(request)
    assert len(result['added']) == len(result['excluded']) == 1
    assert policy.evaluate.call_count == 2
    assert result['added'][0]['replace_source'] is False
    assert processor.get_queue()['pending_count'] == 1
    encoder.encode.assert_not_called()
    processor.get_queue()
    encoder.encode.assert_not_called()
    processor.queue.tick(0)
    processor.queue.tick(180)
    encoder.encode.assert_called_once()
    processor.queue.tick(5)
    assert processor.get_queue()['history'][0]['status'] == 'completed'
    encoder.encode.assert_called_once()


def test_preset_delegation_and_snapshot_independence(movie_payload):
    processor = seed_processor()
    service = processor.presets
    processor.presets = Mock(wraps=service)
    job = processor.queue_encode(EnqueueRequest(**movie_payload))['added'][0]
    duplicate = processor.duplicate_preset(movie_payload['preset_id'])
    processor.presets.duplicate.assert_called_once_with(movie_payload['preset_id'])
    edited = PresetSettings(**duplicate.model_dump(exclude={'id'})).model_copy(update={'name': 'My own name'})
    updated = processor.update_preset(duplicate.id, edited)
    processor.presets.update.assert_called_once_with(duplicate.id, edited)
    stored = service.repository.get_by_id(updated.id)
    assert stored is not None
    assert stored.name == 'My own name'
    assert processor.get_queue()['lanes'][0]['queued'][0]['preset'] == job['preset']
    assert updated.origin == 'custom'


def test_fake_encoder_contract(movie_payload):
    encoder = FakeEncoder()
    assert isinstance(encoder, Encoder)
    processor = seed_processor()
    job = QueueJob(**processor.queue_encode(EnqueueRequest(**movie_payload))['added'][0])
    before = job.model_dump()
    progress = []
    result = encoder.encode(job, progress.append)
    assert result.simulated and result.job_id == job.id
    assert progress == [100]
    assert job.model_dump() == before
    encoder.stop(job.id)


def test_fake_discovery_contracts_do_not_touch_media():
    processor = seed_processor()
    assert isinstance(processor.probe, ProbeService)
    assert isinstance(processor.scanner, MediaScanner)
    library = processor.scan_library()
    source = library.movies[1]
    assert processor.probe is not None
    probed = cast(Movie, processor.probe.inspect(source.path))
    assert probed == source and probed is not source
    probed.name = 'Changed locally'
    assert cast(Movie, processor.probe.inspect(source.path)).name == source.name
    with pytest.raises(NotFound):
        processor.probe.inspect('/media/not-in-seed.mkv')


@pytest.mark.parametrize('error,status', [(NotFound('missing'), 404), (Conflict('active'), 409), (InvalidOperation('invalid'), 422)])
def test_application_errors_are_mapped_by_http(runtime, error, status):
    app, client = runtime
    replacement = Mock(spec=MediaProcessor)
    replacement.get_summary.side_effect = error
    app.dependency_overrides[get_media_processor] = lambda: replacement
    response = client.get('/api/summary')
    assert response.status_code == status
    assert response.json() == {'detail': str(error)}


def test_interlaced_resolution_matches_spatial_applicability(client, catalog):
    item = catalog.find('top-gear-s14e01', 'show').item
    assert item.resolution == '1080i' and item.interlaced
    assert spatial_resolution(item) == '1080p'
    result = client.post('/api/eligibility', json={
        'scope': 'show', 'media_id': item.id, 'preset_id': 'show-streaming-quality',
    }).json()
    assert not result['eligible']
    assert any('deinterlacing' in reason for reason in result['reasons'])
    assert not any('resolution' in reason for reason in result['reasons'])


@pytest.mark.parametrize('block', [False, True])
def test_active_cancellation_reaches_encoder(runtime, movie_payload, block):
    app, client = runtime
    processor = app.state.media_processor
    encoder = Mock(wraps=FakeEncoder())
    processor.queue.worker.encoder = encoder
    job = processor.queue_encode(EnqueueRequest(**movie_payload))['added'][0]
    processor.queue.tick(0)
    if block:
        client.patch('/api/tags', json={'targets': [{'kind': 'movie', 'id': job['media_id']}], 'tags': ['Preserve Video']})
    else:
        processor.stop_job(job['id'])
    encoder.stop.assert_called_once_with(job['id'])
    processor.queue.tick(200)
    encoder.encode.assert_not_called()
