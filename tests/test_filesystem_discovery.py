import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.models.inventory import InventoryState
from app.services.errors import InvalidOperation
from app.services.ffprobe import FFprobeService, parse_ffprobe
from app.services.filesystem_scanner import FilesystemScanner


def scan(client):
    response = client.post('/api/library/scan')
    if response.status_code != 202:
        return response
    for _ in range(500):
        response = client.get('/api/library/scan')
        if response.json()['state'] != 'running':
            return response
        time.sleep(.01)
    pytest.fail('Background scan did not finish')


@pytest.fixture
def roots(tmp_path: Path):
    movies = tmp_path / 'movies'
    shows = tmp_path / 'shows'
    (movies / 'Comedy').mkdir(parents=True)
    (shows / 'Example Show' / 'Season 01').mkdir(parents=True)
    (movies / 'Comedy' / 'Film (1982).mkv').write_bytes(b'fixture movie')
    (shows / 'Example Show' / 'Season 01' / 'Show_S01E01_episode.mkv').write_bytes(b'fixture episode')
    return movies, shows


@pytest.fixture
def config(tmp_path, roots):
    movies, shows = roots
    return Settings(media_backend='filesystem', movies_root=movies, shows_root=shows,
                    database_path=tmp_path / 'app-state' / 'state.sqlite3', ffprobe_binary='not-installed-ffprobe')


@pytest.fixture
def probe(monkeypatch):
    facts = parse_ffprobe(json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text()))
    mock = Mock(side_effect=lambda path: facts.model_copy(deep=True))
    monkeypatch.setattr(FFprobeService, 'inspect', mock)
    return mock


def test_recursive_discovery_stat_hardlinks_and_symlink_exclusion(roots, tmp_path):
    movies, _ = roots
    original = movies / 'Comedy' / 'Film (1982).mkv'
    linked = movies / 'linked.MKV'
    os.link(original, linked)
    (movies / 'ignored.txt').write_text('not media')
    (movies / 'link.mkv').symlink_to(original)
    (movies / 'external-dir').symlink_to(tmp_path, target_is_directory=True)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [original, linked]}
    scan, errors = FilesystemScanner().scan(movies, 'movie', InventoryState())
    assert scan.status == 'complete' and not errors and len(scan.files) == 2
    for item in scan.files:
        info = (movies / item.relative_path).stat()
        assert item.filesystem_id == str(info.st_dev) and item.inode == info.st_ino
        assert item.hardlinks == 2 and item.size == info.st_size
        assert item.ctime_ns == info.st_ctime_ns and item.mtime_ns == info.st_mtime_ns
        assert item.fingerprints.full is None and item.fingerprints.sample is None
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


def test_real_inventory_reaches_ui_api_and_keeps_seed_mode_separate(config, probe):
    app = create_app(config=config)
    with TestClient(app) as client:
        assert client.get('/healthz').json()['media_backend'] == 'filesystem'
        assert client.get('/api/library').json() == {'movies': [], 'shows': []}
        response = scan(client)
        assert response.status_code == 200
        report = response.json()
        assert report['state'] == 'completed' and report['backend'] == 'filesystem'
        assert all(r['status'] == 'complete' for r in report['roots'])
        assert probe.call_count == 2
        library = client.get('/api/library').json()
        movie = library['movies'][0]
        assert 'Film (1982)' in movie['name'] and movie['hdr'] is None
        assert movie['probe']['streams'][3]['kind'] == 'subtitle'
        assert movie['audio'][1]['bitrate'] is None
        assert library['shows'][0]['name'] == 'Example Show'
        episode = library['shows'][0]['seasons'][0]['episodes'][0]
        assert episode['season'] == 1 and episode['episode'] == 1
        for path in ['/movies', '/shows', '/settings', f"/shows/{library['shows'][0]['id']}"]:
            assert client.get(path).status_code == 200
        assert 'Technical metadata' in client.get('/movies').text
        assert 'subrip' in client.get('/movies').text
        assert 'Read-only filesystem' in client.get('/settings').text
        eligible = client.post('/api/eligibility', json={'scope': 'movie', 'media_id': movie['id'], 'preset_id': 'movie-streaming-quality'}).json()
        assert not eligible['eligible'] and any('Read-only' in r for r in eligible['reasons'])
        assert client.post('/api/queue', json={'scope': 'movie', 'media_ids': [movie['id']], 'preset_id': 'movie-streaming-quality'}).status_code == 422
        scan(client)
        assert probe.call_count == 2  # unchanged sources reuse persisted probe facts
        assert client.get('/api/library').json()['movies'][0]['id'] == movie['id']


def test_restart_persistence_rename_and_real_tags(config, probe):
    with TestClient(create_app(config=config)) as client:
        scan(client)
        movie = client.get('/api/library').json()['movies'][0]
        target = {'kind': 'movie', 'id': movie['id']}
        assert client.patch('/api/tags', json={'targets': [target], 'tags': ['Preserve Audio']}).status_code == 200
    path = Path(movie['path'])
    path.rename(path.with_name('Renamed.mkv'))  # simulate an external rename, not scanner behaviour
    with TestClient(create_app(config=config)) as client:
        assert client.get('/api/library').json()['movies'][0]['id'] == movie['id']
        scan(client)
        renamed = client.get('/api/library').json()['movies'][0]
        assert renamed['id'] == movie['id'] and renamed['revision_id'] == movie['revision_id']
        assert renamed['media_id'] == movie['media_id'] and renamed['tags'] == ['Preserve Audio']
        assert Path(renamed['path']).name == 'Renamed.mkv'


def test_unavailable_root_retains_inventory_complete_scan_marks_missing(config, probe):
    assert config.movies_root is not None
    movies = config.movies_root
    with TestClient(create_app(config=config)) as client:
        scan(client)
        offline = movies.with_name('offline')
        movies.rename(offline)
        report = scan(client).json()
        assert report['roots'][0]['status'] == 'unavailable'
        assert len(client.get('/api/library').json()['movies']) == 1
        offline.rename(movies)
        next(movies.rglob('*.mkv')).unlink()  # simulate removal by another application
        report = scan(client).json()
        assert len(report['roots'][0]['reconciliation']['missing']) == 1
        assert client.get('/api/library').json()['movies'] == []


def test_missing_ffprobe_keeps_files_visible_with_unknowns(config):
    with TestClient(create_app(config=config)) as client:
        report = scan(client).json()
        assert report['roots'][0]['errors'] and report['roots'][0]['discovered'] == 1
        movie = client.get('/api/library').json()['movies'][0]
        assert movie['video_bitrate'] is None and movie['duration_seconds'] is None
        assert movie['hdr'] == 'unknown' and movie['probe'] is None
        assert client.get('/movies').status_code == 200
        assert 'Unknown' in client.get('/movies').text


@pytest.mark.parametrize('name,hdr', [('interlaced', None), ('hdr10', 'hdr10'), ('dynamic_hdr', 'dolby_vision')])
def test_probe_bridge_preserves_interlace_and_hdr_safety(config, monkeypatch, name, hdr):
    facts = parse_ffprobe(json.loads((PROJECT_ROOT / f'fixtures/ffprobe/{name}.json').read_text()))
    monkeypatch.setattr(FFprobeService, 'inspect', lambda self, path: facts.model_copy(deep=True))
    with TestClient(create_app(config=config)) as client:
        scan(client)
        movie = client.get('/api/library').json()['movies'][0]
        assert movie['hdr'] == hdr
        result = client.post('/api/eligibility', json={'scope': 'movie', 'media_id': movie['id'], 'preset_id': 'movie-streaming-quality'}).json()
        assert not result['eligible']
        if name == 'interlaced':
            assert movie['resolution'] == '1080i'
            assert 'Interlaced source requires deinterlacing; no validated pipeline is enabled' in result['reasons']
            assert not any('resolution is not supported' in r for r in result['reasons'])
        elif name == 'hdr10':
            assert any('uncertain' in r for r in result['reasons'])
        else:
            assert any('Dolby Vision' in r for r in result['reasons'])


def test_probe_failure_after_change_does_not_reuse_old_metadata(config, probe):
    with TestClient(create_app(config=config)) as client:
        scan(client)
        old = client.get('/api/library').json()['movies'][0]
        Path(old['path']).write_bytes(b'new external replacement with different content')
        probe.side_effect = InvalidOperation('corrupt file')
        scan(client)
        new = client.get('/api/library').json()['movies'][0]
        assert new['id'] == old['id'] and new['revision_id'] != old['revision_id']
        assert new['probe'] is None


def test_seed_mode_never_executes_probe(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', '/not-a-real-bin-directory')
    probe = Mock(side_effect=AssertionError('must not probe in seed mode'))
    monkeypatch.setattr(FFprobeService, 'inspect', probe)
    with TestClient(create_app(tmp_path / 'state.sqlite3')) as client:
        assert len(client.get('/api/library').json()['movies']) == 3
        assert scan(client).status_code == 422
        assert client.get('/api/library/scan').json()['backend'] == 'seed'
    probe.assert_not_called()


def test_paths_are_configurable_and_no_production_defaults(tmp_path, probe):
    config = Settings(media_backend='filesystem', database_path=tmp_path / 'state.sqlite3', movies_root=None, shows_root=None)
    with TestClient(create_app(config=config)) as client:
        assert client.get('/api/settings').json() == {'movies_path': '', 'shows_path': ''}
        assert scan(client).status_code == 422
        assert client.put('/api/settings', json={'movies_path': 'relative', 'shows_path': ''}).status_code == 422
        movies = tmp_path / 'library'
        movies.mkdir()
        (movies / 'Film.mkv').write_bytes(b'fixture')
        assert client.put('/api/settings', json={'movies_path': str(movies), 'shows_path': ''}).status_code == 200
        assert scan(client).status_code == 200
        assert len(client.get('/api/library').json()['movies']) == 1
        # Configuration must not put app state inside media or overlap roots.
        assert client.put('/api/settings', json={'movies_path': str(tmp_path), 'shows_path': ''}).status_code == 422
        assert client.put('/api/settings', json={'movies_path': str(movies), 'shows_path': str(movies)}).status_code == 422


def test_scan_failure_in_subdirectory_never_marks_whole_root_missing(config, probe, monkeypatch):
    with TestClient(create_app(config=config)) as client:
        scan(client)
        walk = os.walk
        def failing_walk(root, **kwargs):
            if root == config.movies_root:
                kwargs['onerror'](PermissionError('subdirectory unavailable'))
                return iter([])
            return walk(root, **kwargs)
        monkeypatch.setattr(os, 'walk', failing_walk)
        report = scan(client).json()
        assert report['roots'][0]['status'] == 'partial'
        assert not report['roots'][0]['reconciliation']['missing']
        assert len(client.get('/api/library').json()['movies']) == 1


def test_content_change_during_probe_discards_inconsistent_facts(config, probe):
    facts = parse_ffprobe(json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text()))
    def changing_probe(path):
        path.write_bytes(b'external replacement during probe')
        return facts
    probe.side_effect = changing_probe
    with TestClient(create_app(config=config)) as client:
        report = scan(client).json()
        assert 'changed during probing' in report['roots'][0]['errors'][0]
        assert client.get('/api/library').json()['movies'][0]['probe'] is None


def test_scanner_does_not_hash_or_probe_unsupported_files(roots):
    movies, _ = roots
    (movies / 'nested').mkdir()
    (movies / 'nested' / 'another.MP4').write_bytes(b'fixture')
    (movies / 'subtitle.srt').write_text('external subtitles not yet discovered')
    report, _ = FilesystemScanner().scan(movies, 'movie', InventoryState())
    assert {item.relative_path for item in report.files} == {'Comedy/Film (1982).mkv', 'nested/another.MP4'}
    assert len({item.media_id for item in report.files}) == 2
    assert all(item.probe is None and item.fingerprints.full is None for item in report.files)
