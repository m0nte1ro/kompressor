import json
import shutil
import subprocess
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config import PROJECT_ROOT
from app.services.errors import InvalidOperation
from app.services.ffprobe import FFprobeService, parse_ffprobe


@pytest.fixture
def probe_payload():
    return json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text())


def fixture(name):
    return parse_ffprobe(json.loads((PROJECT_ROOT / f'fixtures/ffprobe/{name}.json').read_text()))


def test_progressive_streams_chapters_and_metadata():
    result = fixture('progressive')
    video = result.streams[0]
    assert result.duration_seconds == 120.5 and result.container == 'matroska,webm'
    assert video.resolution_class == 1080 and video.scan_type == 'progressive'
    assert video.frame_rate == '24000/1001' and video.bitrate == 12000000
    assert video.color_range == 'tv'
    assert video.hdr is not None and video.hdr.classify().base == 'sdr'
    assert video.hdr.bit_depth == 8
    assert result.streams[1].channel_layout == '5.1(side)'
    assert result.streams[2].language == 'por' and result.streams[2].bitrate is None
    assert result.streams[2].dispositions['comment']
    assert result.streams[3].kind == 'subtitle' and result.streams[3].dispositions['forced']
    assert result.streams[4].kind == 'subtitle'
    assert result.streams[5].kind == 'attachment'
    assert result.chapters[0].title == 'Chapter one'


def test_interlaced_is_spatially_1080():
    video = fixture('interlaced').streams[0]
    assert (video.width, video.height, video.resolution_class) == (1920, 1080, 1080)
    assert video.scan_type == 'interlaced' and video.field_order == 'top_first'


def test_hdr10_preserves_raw_signalling_and_sample_uncertainty():
    video = fixture('hdr10').streams[0]
    hdr = video.hdr
    assert hdr is not None
    assert hdr.classify().base == 'hdr10' and hdr.classify().uncertain
    assert hdr.matrix == 'bt2020nc' and hdr.bit_depth == 10
    assert hdr.mastering_display == {'max_luminance': '1000/1', 'min_luminance': '1/10000'}
    assert hdr.content_light == {'max_content': 1000, 'max_average': 400}
    assert hdr.hdr10plus_metadata is None and hdr.dolby_vision_rpu is None


def test_dv_and_hdr10plus_evidence_can_coexist():
    hdr = fixture('dynamic_hdr').streams[0].hdr
    assert hdr is not None
    result = hdr.classify()
    assert result.dolby_vision and result.hdr10plus
    assert hdr.dolby_vision_config is not None and hdr.dolby_vision_config['dv_profile'] == 8


def test_unknown_values_are_not_guessed(probe_payload):
    probe_payload['format'] = {'format_name': 'matroska', 'bit_rate': '20000000'}
    probe_payload['streams'] = [{'index': 0, 'codec_type': 'video', 'pix_fmt': 'yuv420p10le', 'avg_frame_rate': '0/0', 'bit_rate': 'N/A'}]
    probe_payload['frames'] = []
    result = parse_ffprobe(probe_payload)
    video = result.streams[0]
    assert result.duration_seconds is None and result.container_bitrate == 20000000
    assert video.width is None and video.height is None
    assert video.scan_type == 'unknown' and video.frame_rate is None and video.bitrate is None
    assert video.hdr is not None and video.hdr.classify().base == 'unknown'
    assert video.codec == 'unknown'


def test_legacy_yuvj_probe_infers_full_colour_range(probe_payload):
    probe_payload["streams"][0]["pix_fmt"] = "yuvj420p"
    probe_payload["streams"][0].pop("color_range", None)
    video = parse_ffprobe(probe_payload).streams[0]
    assert video.color_range == "pc"


def test_mixed_frames_override_progressive_stream_flag(probe_payload):
    probe_payload['frames'].append({'stream_index': 0, 'interlaced_frame': 1, 'top_field_first': 0})
    video = parse_ffprobe(probe_payload).streams[0]
    assert video.scan_type == 'mixed'


def test_probe_command_is_argument_list_read_only_bounded_and_normalized(tmp_path, monkeypatch, probe_payload):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(probe_payload), ''))
    monkeypatch.setattr(subprocess, 'run', run)
    path = tmp_path / 'name with spaces;$(anything).mkv'
    result = FFprobeService('/custom/ffprobe', timeout=3).inspect(path)
    command = run.call_args.args[0]
    assert command[0] == '/custom/ffprobe' and command[-1] == str(path)
    assert '-read_intervals' in command and '-protocol_whitelist' in command
    assert run.call_args.kwargs['timeout'] == 3
    assert not run.call_args.kwargs.get('shell', False)
    assert result.streams[0].codec == 'h264'


@pytest.mark.parametrize('error', [FileNotFoundError(), subprocess.TimeoutExpired('ffprobe', 1), PermissionError()])
def test_probe_execution_errors_are_application_errors(tmp_path, monkeypatch, error):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=error))
    with pytest.raises(InvalidOperation):
        FFprobeService().inspect(tmp_path / 'movie.mkv')


@pytest.mark.parametrize('stdout,returncode', [('not json', 0), ('{}', 1), ('[]', 0)])
def test_invalid_probe_output(tmp_path, monkeypatch, stdout, returncode):
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], returncode, stdout, 'failed')))
    with pytest.raises(InvalidOperation):
        FFprobeService().inspect(tmp_path / 'movie.mkv')


@pytest.mark.skipif(shutil.which('ffprobe') is None, reason='ffprobe is optional and not installed')
def test_real_ffprobe_when_installed(tmp_path: Path):
    # Standard-library uncompressed fixture generation; never run an encoder.
    path = tmp_path / 'sample.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\x00\x00' * 800)
    before = path.read_bytes()
    result = FFprobeService().inspect(path)
    assert result.streams[0].kind == 'audio' and result.streams[0].codec == 'pcm_s16le'
    assert path.read_bytes() == before
