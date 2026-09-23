import json
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.services.ffprobe import FFprobeService, parse_ffprobe
from app.services.estimation import audio_plan
from app.models.preset import CompressionPreset
from app.models.media import AudioTrack
from test_filesystem_discovery import scan


def test_saved_roots_override_environment_and_change_both_next_scan_roots(tmp_path, monkeypatch):
    old_movies, old_shows, movies, shows = [tmp_path / name for name in ('oldmovies', 'oldshows', 'movies', 'shows')]
    for root in (old_movies, old_shows, movies, shows):
        root.mkdir()
    (old_movies / 'Old.mkv').write_bytes(b'old')
    (movies / 'New.mkv').write_bytes(b'new')
    (shows / 'Series').mkdir()
    (shows / 'Series' / 'Series.S01E01.mkv').write_bytes(b'episode')
    facts = parse_ffprobe(json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text()))
    inspected = Mock(return_value=facts)
    monkeypatch.setattr(FFprobeService, 'inspect', inspected)
    config = Settings(media_backend='filesystem', movies_root=old_movies, shows_root=old_shows,
                      database_path=tmp_path / 'state.sqlite3', app_name='My Kompressor')
    with TestClient(create_app(config=config)) as client:
        assert client.get('/healthz').json()['app'] == 'My Kompressor'
        assert client.put('/api/settings', json={'movies_path': str(movies), 'shows_path': str(shows)}).status_code == 200
        assert scan(client).json()['state'] == 'completed'
        assert client.get('/api/summary').json()['episodes'] == 1
        assert [m['name'] for m in client.get('/api/library').json()['movies']] == ['New']
        assert all(path.args[0].is_relative_to(movies) or path.args[0].is_relative_to(shows) for path in inspected.call_args_list)
        runtime = client.get('/api/settings/runtime').json()
        assert 'next_scan' in runtime['paths_apply_timing'] and not runtime['encoding_enabled']
    # Even overlapping env defaults must not override already-saved valid paths.
    config.movies_root = tmp_path
    config.shows_root = tmp_path
    with TestClient(create_app(config=config)) as client:
        assert client.get('/api/settings').json() == {'movies_path': str(movies), 'shows_path': str(shows)}
        assert scan(client).json()['state'] == 'completed'


def test_ffprobe_configuration_reaches_subprocess(tmp_path, monkeypatch):
    import subprocess
    movie_root = tmp_path / 'movies'
    movie_root.mkdir()
    (movie_root / 'Movie.mkv').write_bytes(b'fixture')
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, '{"format":{},"streams":[]}', ''))
    monkeypatch.setattr(subprocess, 'run', execute)
    config = Settings(media_backend='filesystem', movies_root=movie_root, database_path=tmp_path / 'state.sqlite3',
                      ffprobe_binary='/custom/probe', ffprobe_timeout=7)
    with TestClient(create_app(config=config)) as client:
        assert scan(client).json()['state'] == 'completed'
        assert execute.call_args.args[0][0] == '/custom/probe'
        assert execute.call_args.kwargs['timeout'] == 7
        runtime = client.get('/api/settings/runtime').json()
        assert runtime['startup']['ffprobe_binary'] == '/custom/probe'
        assert 'requires restart' in client.get('/settings').text


def test_preserve_audio_overrides_downmix_and_stereo_conversion_uses_stereo_target(catalog):
    preset = catalog.preset('show-streaming-quality')
    preset = CompressionPreset.model_validate({**preset.model_dump(), 'audio_conversion_policy': 'efficient',
        'target_audio_bitrate': 640000, 'stereo_audio_bitrate': 192000,
        'efficient_audio_rules': {'channel_handling': 'downmix_stereo'}})
    item = catalog.find('modern-family-s03e04', 'show').item.model_copy(deep=True)
    item.audio = [AudioTrack(codec='truehd', channels=6, bitrate=2000000)]
    copied = audio_plan(item, preset, True)[0]
    assert copied['action'] == 'copy' and copied['channels'] == 6 and copied['bitrate'] == 2000000
    encoded = audio_plan(item, preset, False)[0]
    assert encoded['channels'] == 2 and encoded['codec'] == 'aac' and encoded['bitrate'] == 192000
