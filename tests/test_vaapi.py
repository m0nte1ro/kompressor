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
from tests.test_real_encoding import build_real_app, movie_item, qsv_preset, queue_job


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
    ('vc1', 'Advanced', 'yuv420p', True),
    ('mpeg4', 'Advanced Simple Profile', 'yuv420p', False),
])
def test_decode_selection(codec, profile, pixel, expected):
    stream = StreamFacts(index=0, kind='video', codec=codec, profile=profile, pixel_format=pixel)
    assert vaapi.hardware_decode(stream) is expected


@pytest.mark.parametrize('hardware', [False, True])
@pytest.mark.parametrize('depth', [8, 10])
@pytest.mark.parametrize('mode', ['icq', 'abr'])
def test_gpu_command_pipeline(facts, tmp_path, hardware, depth, mode):
    facts.streams[0].profile = 'High' if hardware else None
    preset = qsv_preset().model_copy(update={
        'output_bit_depth': depth, 'rate_control': mode, 'hdr_support': 'sdr_only',
        'quality_value': 23 if mode == 'icq' else None,
        'target_video_bitrate': 4_000_000 if mode == 'abr' else None})
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(preset, backend='qsv'),
        Path('/source.mkv'), tmp_path / 'out.mkv', facts, Path('/dev/dri/renderD128'))
    assert command[command.index('-init_hw_device') + 1] == 'vaapi=va:/dev/dri/renderD128'
    assert ('-hwaccel:0' in command) is hardware
    if hardware:
        assert command.index('-hwaccel:0') < command.index('-i')
    expected_filter = ('scale_vaapi=format=' + ('p010' if depth == 10 else 'nv12') if hardware
                       else 'format=' + ('p010le' if depth == 10 else 'nv12') + ',hwupload')
    assert command[command.index('-filter:v:0') + 1] == expected_filter
    assert command[command.index('-profile:v:0') + 1] == ('main10' if depth == 10 else 'main')
    assert command[command.index('-rc_mode:v:0') + 1] == ('ICQ' if mode == 'icq' else 'VBR')
    assert ('-global_quality:v:0' in command) is (mode == 'icq')
    assert ('-b:v:0' in command) is (mode == 'abr')
    assert not {'-preset:v:0', '-crf:v:0', '-low_power', 'hevc_qsv'} & set(command)


def test_gpu_cover_art_mapping_subtitles_and_unknown_colour(facts, tmp_path):
    facts.streams[0].index = 8
    facts.streams[0].profile = 'High'
    facts.streams[0].hdr = None
    cover = StreamFacts(index=0, kind='video', codec='mjpeg', dispositions={'attached_pic': True})
    facts.streams.insert(0, cover)
    facts.streams[4].codec = 'mov_text'
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(qsv_preset(), backend='qsv'),
        Path('/source.mkv'), tmp_path / 'out.mkv', facts, Path('/dev/dri/renderD128'))
    mapped = [command[i + 1] for i, flag in enumerate(command) if flag == '-map']
    assert mapped == ['0:8', '0:0', '0:1', '0:2', '0:3', '0:4', '0:5']
    assert '-hwaccel:8' in command and '-hwaccel:0' not in command
    assert command[command.index('-c:s:0') + 1] == 'srt'
    assert command[command.index('-c') + 1] == 'copy'
    assert '-c:v:1' not in command and '-filter:v:1' not in command
    assert not {'-color_primaries:v:0', '-color_trc:v:0', '-colorspace:v:0', 'None', ''} & set(command)


@pytest.mark.parametrize('failure', ['icq', 'wrong_profile', 'no_frames', 'timeout'])
def test_smoke_failure_is_explicit_and_cleans_workspace(tmp_path, monkeypatch, failure):
    (tmp_path / 'jobs').mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'ffmpeg':
            Path(command[-1]).write_bytes(b'partial')
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, 15)
            return subprocess.CompletedProcess(command, int(failure == 'icq'), '', 'ICQ not supported' if failure == 'icq' else '')
        stream = {'codec_name': 'hevc', 'profile': 'Main' if failure == 'wrong_profile' else 'Main 10',
                  'pix_fmt': 'yuv420p10le', 'nb_read_frames': '0' if failure == 'no_frames' else '10'}
        return subprocess.CompletedProcess(command, 0, json.dumps({'streams': [stream]}), '')
    monkeypatch.setattr(vaapi.subprocess, 'run', run)
    ready, reason = vaapi.smoke_check('ffmpeg', Path('/dev/dri/renderD128'), 'ffprobe', tmp_path)
    assert not ready and reason is not None and 'ICQ' in reason
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
    job = queue_job(qsv_preset(), backend='qsv')
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
        job.backend = 'qsv'
        job.preset = qsv_preset().model_copy(update={'scope': 'movie', 'name': 'My personal preset'})
        queue.repository.save(job)
        queue.recover(backends={'qsv'})
        queue.revalidate()
        assert queue.claim_next('qsv') is None
        persisted = next(j for j in queue.repository.get_all() if j.id == added['id'])
        assert persisted.status == 'queued' and persisted.preset.name == 'My personal preset'
        assert queue.snapshot()['lanes'][0]['available'] is True
        # The same persisted job can be picked up when the GPU returns.
        queue.supported_backends = frozenset({'cpu', 'qsv'})
        assert queue.claim_next('qsv') is not None


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ''
    def handle_data(self, data):
        self.text += data


@pytest.mark.parametrize('url', ['/movies', '/shows', '/queue', '/history', '/settings'])
def test_ui_uses_gpu_labels_with_legacy_lane_keys(client, url):
    response = client.get(url)
    assert response.status_code == 200
    visible = VisibleText()
    visible.feed(response.text)
    assert 'QSV' not in visible.text and 'GPU' in visible.text
    assert 'id="qsv-state"' in response.text


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
    assert status['qsv_available'] is False and status['hevc_vaapi_available'] is False
    assert 'GPU' in status['qsv_unavailable_reason']
