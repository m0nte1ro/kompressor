import json
import time
from threading import Event

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.services.ffprobe import FFprobeService, parse_ffprobe


def test_slow_probe_does_not_block_http_and_publishes_discovery(tmp_path, monkeypatch):
    root = tmp_path / 'movies'
    root.mkdir()
    for i in range(205):
        (root / f'Film {i:03}.mkv').write_bytes(b'fixture')
    entered, release = Event(), Event()
    facts = parse_ffprobe(json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text()))
    def slow_probe(self, path):
        entered.set()
        assert release.wait(10)
        return facts
    monkeypatch.setattr(FFprobeService, 'inspect', slow_probe)
    config = Settings(media_backend='filesystem', movies_root=root, database_path=tmp_path / 'state.sqlite3')
    with TestClient(create_app(config=config)) as client:
        try:
            response = client.post('/api/library/scan')
            assert response.status_code == 202 and response.json()['state'] == 'running'
            assert entered.wait(5)
            assert client.post('/api/library/scan').status_code == 409
            assert client.get('/healthz').status_code == 200
            assert client.get('/api/queue').status_code == 200
            assert client.get('/api/summary').json()['movies'] == 205
            library = client.get('/api/library').json()
            assert library['movies'][0]['probe'] is None
            assert 'Film 000' in client.get('/movies').text
            assert client.get('/api/library/scan').json()['phase'] == 'probing'
        finally:
            release.set()
        report = None
        for _ in range(500):
            report = client.get('/api/library/scan').json()
            if report['state'] != 'running':
                break
            time.sleep(.01)
        assert report is not None
        assert report['state'] == 'completed'
        assert report['roots'][0]['probed'] == 205
        assert 'Technical metadata' in client.get('/movies').text
        assert 'discovery.js' in client.get('/static/app.js').text
        assert client.get('/api/settings/runtime').json()['startup_apply_timing'] == 'restart_required'


def test_initial_walk_publishes_before_enumeration_finishes(tmp_path, monkeypatch):
    import os
    root = tmp_path / 'movies'
    root.mkdir()
    for i in range(100):
        (root / f'{i:03}.mkv').write_bytes(b'fixture')
    entered, release = Event(), Event()
    real_walk = os.walk
    def slow_walk(path, **kwargs):
        yield from real_walk(path, **kwargs)
        entered.set()
        assert release.wait(10)
    monkeypatch.setattr(os, 'walk', slow_walk)
    monkeypatch.setattr(FFprobeService, 'inspect', lambda *args: parse_ffprobe({'format': {}, 'streams': []}))
    config = Settings(media_backend='filesystem', movies_root=root, database_path=tmp_path / 'state.sqlite3')
    with TestClient(create_app(config=config)) as client:
        try:
            assert client.post('/api/library/scan').status_code == 202
            assert entered.wait(5)
            assert client.get('/api/summary').json()['movies'] == 100
            assert client.get('/api/library/scan').json()['phase'] == 'discovering'
        finally:
            release.set()


def test_failed_background_scan_is_reported_and_lock_released(tmp_path, monkeypatch):
    root = tmp_path / 'movies'
    root.mkdir()
    app = create_app(config=Settings(media_backend='filesystem', movies_root=root, database_path=tmp_path / 'state.sqlite3'))
    with TestClient(app) as client:
        discovery = app.state.media_processor.discovery
        def fail(*args, **kwargs):
            raise RuntimeError('fixture traversal failure')
        monkeypatch.setattr(discovery.scanner, 'scan', fail)
        for _ in range(2):
            assert client.post('/api/library/scan').status_code == 202
            discovery.thread.join(5)
            report = client.get('/api/library/scan').json()
            assert report['state'] == 'failed' and report['error'] == 'fixture traversal failure'


def test_shutdown_stops_background_scan(tmp_path, monkeypatch):
    root = tmp_path / 'movies'
    root.mkdir()
    (root / 'Movie.mkv').write_bytes(b'fixture')
    app = create_app(config=Settings(media_backend='filesystem', movies_root=root,
                                     database_path=tmp_path / 'state.sqlite3'))
    entered = Event()
    facts = parse_ffprobe({'format': {}, 'streams': []})

    def probe_until_shutdown(self, path):
        entered.set()
        assert app.state.media_processor.discovery.stop.wait(5)
        return facts

    monkeypatch.setattr(FFprobeService, 'inspect', probe_until_shutdown)
    with TestClient(app) as client:
        assert client.post('/api/library/scan').status_code == 202
        assert entered.wait(5)
        assert client.get('/api/queue').status_code == 200
    discovery = app.state.media_processor.discovery
    assert not discovery.thread.is_alive()
    assert discovery.status()['state'] == 'cancelled'
