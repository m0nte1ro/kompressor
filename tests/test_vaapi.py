"""VA-API migration regressions; never require a device or media tools."""
import json
import sqlite3
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.models.media import AudioTrack
from app.models.probe import StreamFacts
from app.models.queue import EnqueueRequest
from app.repositories.database import Database
from app.repositories.discovery_fixture import read_snapshot
from app.repositories.inventory import SQLiteInventoryRepository
from app.services.encoding_capability import GPUEncodeCapability
from app.services.ffprobe import parse_ffprobe
from app.services.reconciliation import ReconciliationService
from app.workers import vaapi
from app.workers.ffmpeg import FFmpegEncoder
from tests.test_real_encoding import build_real_app, movie_item, gpu_preset, queue_job


@pytest.fixture
def facts():
    return parse_ffprobe(json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text()))


@pytest.mark.parametrize('codec,profile,pixel,expected', [
    ('h264', 'High', 'yuv420p', True),
    ('h264', 'Constrained Baseline', 'yuv420p', True),
    ('h264', 'High 10', 'yuv420p10le', False),
    ('h264', None, 'yuv420p', False),
    ('h264', 'High', 'yuvj420p', False),
    ('hevc', 'Main', 'yuv420p', True),
    ('hevc', 'Main 10', 'yuv420p10le', True),
    ('hevc', 'Rext', 'yuv420p10le', False),
    ('vp9', 'Profile 2', 'yuv420p10le', True),
    ('vp9', 'Profile 2', 'yuv420p12le', False),
    ('mpeg2video', 'Main', 'yuv420p', True),
    ('vc1', 'Advanced', 'yuv420p', False),
    ('vc1', 'Main', 'yuv420p', True),
    ('mpeg4', 'Advanced Simple Profile', 'yuv420p', False),
])
def test_decode_selection(codec, profile, pixel, expected):
    stream = StreamFacts(index=0, kind='video', codec=codec, profile=profile, pixel_format=pixel)
    assert vaapi.hardware_decode(stream) is expected


@pytest.mark.parametrize('hardware', [False, True])
@pytest.mark.parametrize('depth', [8, 10])
@pytest.mark.parametrize('mode', ['icq', 'qvbr', 'abr'])
def test_gpu_command_pipeline(facts, tmp_path, hardware, depth, mode):
    facts.streams[0].profile = 'High' if hardware else None
    preset = gpu_preset().model_copy(update={
        'output_bit_depth': depth, 'rate_control': mode, 'hdr_support': 'sdr_only',
        'quality_value': 23 if mode in {'icq', 'qvbr'} else None,
        'target_video_bitrate': 4_000_000 if mode == 'abr' else None,
        'qvbr_bitrates_by_resolution': gpu_preset().qvbr_bitrates_by_resolution if mode == 'qvbr' else {}})
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(preset, backend='gpu'),
        Path('/source.mkv'), tmp_path / 'out.mkv', facts, Path('/dev/dri/renderD128'))
    assert command[command.index('-init_hw_device') + 1] == 'vaapi=va:/dev/dri/renderD128'
    assert ('-hwaccel:0' in command) is hardware
    if hardware:
        assert command.index('-hwaccel:0') < command.index('-i')
    expected_filter = ('scale_vaapi=format=' + ('p010' if depth == 10 else 'nv12') if hardware
                       else 'format=' + ('p010le' if depth == 10 else 'nv12') + ',hwupload')
    assert command[command.index('-filter:v:0') + 1] == expected_filter
    assert command[command.index('-profile:v:0') + 1] == ('main10' if depth == 10 else 'main')
    assert command[command.index('-rc_mode:v:0') + 1] == (mode.upper() if mode in {'icq', 'qvbr'} else 'VBR')
    assert ('-global_quality:v:0' in command) is (mode in {'icq', 'qvbr'})
    assert ('-b:v:0' in command) is (mode in {'qvbr', 'abr'})
    assert not {'-preset:v:0', '-crf:v:0', '-low_power', 'hevc_qsv'} & set(command)


def test_gpu_cover_art_mapping_subtitles_and_unknown_colour(facts, tmp_path):
    facts.streams[0].index = 8
    facts.streams[0].profile = 'High'
    facts.streams[0].hdr = None
    cover = StreamFacts(index=0, kind='video', codec='mjpeg', dispositions={'attached_pic': True})
    facts.streams.insert(0, cover)
    facts.streams[4].codec = 'mov_text'
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(gpu_preset(), backend='gpu'),
        Path('/source.mkv'), tmp_path / 'out.mkv', facts, Path('/dev/dri/renderD128'))
    mapped = [command[i + 1] for i, flag in enumerate(command) if flag == '-map']
    assert mapped == ['0:8', '0:0', '0:1', '0:2', '0:3', '0:4', '0:5']
    assert '-hwaccel:8' in command and '-hwaccel:0' not in command
    assert command[command.index('-c:s:0') + 1] == 'srt'
    assert command[command.index('-c') + 1] == 'copy'
    assert '-c:v:1' not in command and '-filter:v:1' not in command
    assert not {'-color_primaries:v:0', '-color_trc:v:0', '-colorspace:v:0', 'None', ''} & set(command)


@pytest.mark.parametrize('failure', ['qvbr', 'wrong_profile', 'no_frames', 'timeout'])
def test_smoke_failure_is_explicit_and_cleans_workspace(tmp_path, monkeypatch, failure):
    (tmp_path / 'jobs').mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'ffmpeg':
            Path(command[-1]).write_bytes(b'partial')
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 15)
            return subprocess.CompletedProcess(command, int(failure == 'qvbr'), '', 'QVBR not supported' if failure == 'qvbr' else '')
        stream = {'codec_name': 'hevc', 'profile': 'Main' if failure == 'wrong_profile' else 'Main 10',
                  'pix_fmt': 'yuv420p10le', 'nb_read_frames': '0' if failure == 'no_frames' else '10'}
        return subprocess.CompletedProcess(command, 0, json.dumps({'streams': [stream]}), '')
    monkeypatch.setattr(vaapi.subprocess, 'run', run)
    ready, reason = vaapi.smoke_check('ffmpeg', Path('/dev/dri/renderD128'), 'ffprobe', tmp_path)
    assert not ready and reason is not None and 'QVBR' in reason
    assert len([command for command in calls if command[0] == 'ffmpeg']) == 1
    assert not list((tmp_path / 'jobs').iterdir())


def test_profile_is_parsed_persisted_and_v3_migration_preserves_state(tmp_path):
    snapshot = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')
    path = tmp_path / 'state.sqlite3'
    repository = SQLiteInventoryRepository(Database(path))
    ReconciliationService(repository).reconcile(snapshot)
    before = repository.load()
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE streams DROP COLUMN profile')
        connection.execute('PRAGMA user_version=3')
    repository = SQLiteInventoryRepository(Database(path))
    assert repository.load() == before
    with repository.database.read() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 4
    raw = json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text())
    raw['streams'][0]['profile'] = 'High'
    probe = parse_ffprobe(raw)
    record = next(iter(before.files.values()))
    assert repository.attach_probe(record.file_id, record.revision_id,
                                   record.observation.model_copy(update={'probe': probe}))
    restarted = SQLiteInventoryRepository(Database(path)).load()
    restored_probe = restarted.files[record.file_id].observation.probe
    assert restored_probe is not None and restored_probe.streams[0].profile == 'High'


@pytest.mark.parametrize('mutation,reason', [('encoder', 'encoder tag'), ('profile', 'profile'),
                                           ('pixel', 'pixel format'), ('audio_order', 'codec'),
                                           ('stream_order', 'stream order')])
def test_gpu_output_rejects_mismatch(facts, tmp_path, mutation, reason):
    audio = [AudioTrack(codec=s.codec, channels=s.channels, bitrate=s.bitrate, language=s.language)
             for s in facts.streams if s.kind == 'audio']
    item = movie_item(facts, audio=audio)
    job = queue_job(gpu_preset(), backend='gpu')
    output = facts.model_copy(deep=True)
    video = output.streams[0]
    video.codec = 'hevc'
    video.pixel_format = 'yuv420p10le'
    video.profile = 'Main 10'
    assert video.hdr is not None
    video.hdr.bit_depth = 10
    video.metadata['ENCODER'] = 'Lavc61 hevc_vaapi'
    path = tmp_path / 'output.mkv'
    path.write_bytes(b'fixture')
    guard = GPUEncodeCapability()
    assert guard.validate_output(item, job, output, path) == []
    if mutation == 'encoder': video.metadata['ENCODER'] = 'Lavc61 libx265'
    elif mutation == 'profile': video.profile = 'Main'
    elif mutation == 'pixel': video.pixel_format = 'yuv420p'
    elif mutation == 'audio_order':
        output.streams[1].index, output.streams[2].index = 2, 1
    elif mutation == 'stream_order':
        output.streams[1].index, output.streams[3].index = 3, 1
    assert any(reason in error for error in guard.validate_output(item, job, output, path))


def test_unavailable_gpu_retains_legacy_job_and_cpu_keeps_working(tmp_path, monkeypatch, facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope='movie',
            preset_id='movie-streaming-quality'))['added'][0]
        queue = processor.queue
        job = queue.repository.get_all()[0]
        job.backend = 'gpu'
        job.preset = gpu_preset().model_copy(update={'scope': 'movie', 'name': 'My personal preset'})
        queue.repository.save(job)
        queue.recover(backends={'gpu'})
        queue.revalidate()
        assert queue.claim_next('gpu') is None
        persisted = next(j for j in queue.repository.get_all() if j.id == added['id'])
        assert persisted.status == 'queued' and persisted.preset.name == 'My personal preset'
        assert queue.snapshot()['lanes'][0]['available'] is True
        # The same persisted job can be picked up when the GPU returns.
        queue.supported_backends = frozenset({'cpu', 'gpu'})
        assert queue.claim_next('gpu') is not None


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ''
    def handle_data(self, data):
        self.text += data


@pytest.mark.parametrize('url', ['/movies', '/shows', '/queue', '/history', '/settings'])
def test_ui_names_the_gpu_lane_and_never_qsv(client, url):
    response = client.get(url)
    assert response.status_code == 200
    visible = VisibleText()
    visible.feed(response.text)
    assert 'GPU' in visible.text
    assert 'id="gpu-state"' in response.text and 'qsv' not in response.text.lower()


def test_payloads_saved_with_the_legacy_qsv_name_load_as_gpu(tmp_path):
    from app.models.preferences import WorkerLaneSettings, WorkerSettings
    from app.repositories.preferences import SQLitePreferencesRepository
    from app.repositories.sqlite import SQLitePresetRepository, SQLiteQueueRepository
    database = Database(tmp_path / 'legacy.sqlite3')
    preset = json.loads(gpu_preset().model_dump_json()) | {'backend': 'qsv'}
    job = json.loads(queue_job(gpu_preset(), backend='gpu').model_dump_json())
    job.update(backend='qsv', preset=preset)
    lanes = {'timezone': 'UTC', 'cpu': {}, 'qsv': {'paused': True}}
    with database.transaction() as connection:
        connection.execute('INSERT INTO metadata VALUES (?, ?)', ('presets_initialized', 'true'))
        connection.execute('INSERT INTO presets VALUES (?, ?)', (preset['id'], json.dumps(preset)))
        connection.execute('INSERT INTO jobs VALUES (?, ?)', (job['id'], json.dumps(job)))
        connection.execute('INSERT INTO metadata VALUES (?, ?)', ('worker_settings', json.dumps(lanes)))
    assert SQLitePresetRepository(database, []).get_all()[0].backend == 'gpu'
    loaded = SQLiteQueueRepository(database).get_all()[0]
    assert loaded.backend == 'gpu' and loaded.preset.backend == 'gpu'
    settings = SQLitePreferencesRepository(database).get_worker_settings()
    assert settings.gpu == WorkerLaneSettings(paused=True) and settings.cpu == WorkerLaneSettings()
    # Saving again writes only the new name.
    assert 'qsv' not in SQLitePreferencesRepository(database).save_worker_settings(settings).model_dump_json()
    assert WorkerSettings.model_validate({'gpu': {'paused': True}}).gpu.paused


def test_legacy_gpu_device_variable_is_read_only_when_the_new_one_is_unset(tmp_path, monkeypatch):
    from app.config import Settings
    monkeypatch.chdir(tmp_path)  # no .env file here
    monkeypatch.delenv('KOMPRESSOR_GPU_DEVICE', raising=False)
    monkeypatch.setenv('KOMPRESSOR_QSV_DEVICE', '/dev/dri/renderD129')
    assert Settings().gpu_device == Path('/dev/dri/renderD129')
    monkeypatch.setenv('KOMPRESSOR_GPU_DEVICE', '/dev/dri/renderD130')
    assert Settings().gpu_device == Path('/dev/dri/renderD130')


def test_gpu_worker_accepts_the_legacy_qsv_argument(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import worker_main
    monkeypatch.setattr(worker_main, 'settings', SimpleNamespace(database_path=tmp_path / 'state.sqlite3'))
    requested = []
    def fake_processor(settings, *, process_role, worker_backend):
        requested.append(worker_backend)
        return SimpleNamespace(queue=SimpleNamespace(worker=None))
    monkeypatch.setattr(worker_main, 'build_media_processor', fake_processor)
    # Not a real worker in this runtime, so run() stops after selecting the lane.
    assert worker_main.run('qsv') == 2 and worker_main.run('gpu') == 2
    assert requested == ['gpu', 'gpu']
    assert worker_main.run('nvenc') == 2 and requested == ['gpu', 'gpu']


def test_a_second_worker_for_the_same_lane_exits_before_recovery(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import worker_main
    monkeypatch.setattr(worker_main, 'settings', SimpleNamespace(database_path=tmp_path / 'state.sqlite3'))
    built = []
    monkeypatch.setattr(worker_main, 'build_media_processor', lambda *args, **kwargs: built.append(1))
    # A running GPU worker holds its lane (an old qsv unit is the same lane).
    owner = worker_main.acquire_lane(tmp_path / 'state.sqlite3', 'gpu')
    assert owner is not None
    assert worker_main.run('qsv') == worker_main.EXIT_LANE_OWNED
    assert worker_main.run('gpu') == worker_main.EXIT_LANE_OWNED
    # Recovery (inside build_media_processor) never ran for the duplicate.
    assert built == []
    # The CPU lane is independent, and the lane frees up when its owner exits.
    cpu = worker_main.acquire_lane(tmp_path / 'state.sqlite3', 'cpu')
    assert cpu is not None
    owner.close()
    again = worker_main.acquire_lane(tmp_path / 'state.sqlite3', 'gpu')
    assert again is not None
    again.close()
    cpu.close()


def test_no_legacy_encoder_implementation():
    for path in (PROJECT_ROOT / 'app').rglob('*'):
        if path.suffix in {'.py', '.html', '.js'}:
            assert 'hevc_qsv' not in path.read_text(), path


@pytest.mark.parametrize('device_state', ['missing', 'regular_file', 'no_access'])
def test_device_failure_prevents_smoke_and_keeps_cpu_available(tmp_path, monkeypatch, device_state):
    from app.services import encoding_runtime
    from app.workers import ffmpeg
    device = tmp_path / 'renderD128'
    (tmp_path / 'jobs').mkdir()
    if device_state != 'missing':
        device.touch()
    if device_state == 'no_access':
        monkeypatch.setattr(ffmpeg.stat, 'S_ISCHR', lambda mode: True)
        original_access = ffmpeg.os.access
        monkeypatch.setattr(ffmpeg.os, 'access', lambda path, mode: False if path == device else original_access(path, mode))
    monkeypatch.setattr(encoding_runtime, 'executable', lambda binary: binary)
    monkeypatch.setattr(encoding_runtime.FFprobeService, 'runtime_check', lambda binary: (True, None))
    monkeypatch.setattr(FFmpegEncoder, 'runtime_check', lambda binary: (True, None))
    def unexpected_run(*args, **kwargs):
        raise AssertionError('Unavailable GPU must not start any subprocess')
    monkeypatch.setattr(vaapi.subprocess, 'run', unexpected_run)
    status = encoding_runtime.capability_status('ffmpeg', 'ffprobe', tmp_path, {}, device)
    assert status['supported_backends'] == ['cpu']
    assert status['gpu_available'] is False and status['hevc_vaapi_available'] is False
    assert 'GPU' in status['gpu_unavailable_reason']


def test_smoke_report_preserves_first_driver_error_when_ffmpeg_shutdown_is_noisy(
        tmp_path, monkeypatch):
    (tmp_path / 'jobs').mkdir()
    def run(command, **kwargs):
        detail = ('[hevc_vaapi] Requested rate control mode QVBR is not supported\n'
                  + '[ffmpeg] cleanup noise\n' * 150
                  + 'Nothing was written into output file\n')
        return subprocess.CompletedProcess(command, 1, '', detail)
    monkeypatch.setattr(vaapi.subprocess, 'run', run)
    ready, reason = vaapi.smoke_check('ffmpeg', Path('/dev/dri/renderD128'),
                                      'ffprobe', tmp_path)
    assert ready is False and reason is not None
    assert 'Requested rate control mode QVBR is not supported' in reason
    assert 'Nothing was written into output file' in reason
    assert not list((tmp_path / 'jobs').iterdir())


def test_qvbr_preset_requires_both_quality_and_nominal_bitrate():
    from pydantic import ValidationError
    from app.models.preset import CompressionPreset

    preset = gpu_preset()
    assert preset.rate_control == 'qvbr' and preset.qvbr_bitrates_by_resolution['1080p'] == 4_000_000
    for changes in ({'quality_value': None}, {'target_video_bitrate': 4_000_000},
                    {'qvbr_bitrates_by_resolution': {}},
                    {'qvbr_bitrates_by_resolution': {'1080p': 4_000_000}},
                    {'backend': 'cpu'}, {'quality_value': 23.5}):
        with pytest.raises(ValidationError):
            CompressionPreset.model_validate({**preset.model_dump(), **changes})


def test_qvbr_migration_only_updates_untouched_built_ins(tmp_path):
    from app.main import create_app
    from app.models.preset import SUPPORTED_SOURCE_RESOLUTIONS

    path = tmp_path / 'state.sqlite3'
    seeds = [gpu_preset(), gpu_preset(efficient_audio=True)]
    old = [p.model_copy(update={'rate_control': 'icq', 'target_video_bitrate': None,
                                'qvbr_bitrates_by_resolution': {}, 'minimum_source_bitrate': 4_000_000,
                                'source_resolutions': list(SUPPORTED_SOURCE_RESOLUTIONS)}) for p in seeds]
    edited = old[1].model_copy(update={'name': 'My edited GPU preset'})
    database = Database(path)
    with database.transaction() as connection:
        for preset in (old[0], edited):
            connection.execute('INSERT INTO presets VALUES (?, ?)',
                               (preset.id, preset.model_dump_json()))
        connection.executemany('INSERT INTO metadata VALUES (?, ?)', [
            ('presets_initialized', 'true'), ('preset_catalog_v8', 'true')])
    with TestClient(create_app(path, start_workers=False)) as client:
        presets = {p['id']: p for p in client.get('/api/presets').json()}
        assert presets[old[0].id]['rate_control'] == 'qvbr'
        assert presets[old[0].id]['qvbr_bitrates_by_resolution']['576p'] == 1_500_000
        assert presets[old[0].id]['source_resolutions'] == list(SUPPORTED_SOURCE_RESOLUTIONS)
        assert presets[edited.id]['rate_control'] == 'icq'
        assert presets[edited.id]['name'] == 'My edited GPU preset'
    with TestClient(create_app(path, start_workers=False)) as client:
        assert client.get('/api/presets').status_code == 200


@pytest.mark.parametrize('resolution,nominal', [
    (480, 1_000_000), (576, 1_500_000), (720, 2_500_000),
    (1080, 4_000_000), (2160, 16_000_000),
])
def test_qvbr_command_uses_source_resolution_nominal(facts, tmp_path, resolution, nominal):
    facts.streams[0].resolution_class = resolution
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(gpu_preset(), backend='gpu'),
        Path('/source.mkv'), tmp_path / 'out.mkv', facts, Path('/dev/dri/renderD128'))
    assert command[command.index('-b:v:0') + 1] == str(nominal)
    assert command[command.index('-global_quality:v:0') + 1] == '23'


def test_v1_qvbr_builtin_migrates_but_edited_preset_remains(tmp_path):
    from app.main import create_app

    path = tmp_path / 'state.sqlite3'
    seeds = [gpu_preset(), gpu_preset(efficient_audio=True)]
    old = [p.model_copy(update={'target_video_bitrate': 4_000_000,
                                'qvbr_bitrates_by_resolution': {},
                                'source_resolutions': ['1080p'],
                                'minimum_source_bitrate': 4_000_000}) for p in seeds]
    edited = old[1].model_copy(update={'name': 'My tuned GPU preset'})
    with Database(path).transaction() as connection:
        for preset in (old[0], edited):
            connection.execute('INSERT INTO presets VALUES (?, ?)',
                               (preset.id, preset.model_dump_json()))
        connection.executemany('INSERT INTO metadata VALUES (?, ?)', [
            ('presets_initialized', 'true'), ('preset_catalog_v8', 'true'),
            ('preset_gpu_qvbr_v1', 'true')])
    with TestClient(create_app(path, start_workers=False)) as client:
        presets = {p['id']: p for p in client.get('/api/presets').json()}
        assert presets[old[0].id]['qvbr_bitrates_by_resolution']['576p'] == 1_500_000
        assert presets[old[0].id]['source_resolutions'] == ['480p', '576p', '720p', '1080p', '2160p']
        assert presets[edited.id]['name'] == 'My tuned GPU preset'
        assert presets[edited.id]['target_video_bitrate'] == 4_000_000
        assert presets[edited.id]['source_resolutions'] == ['1080p']
