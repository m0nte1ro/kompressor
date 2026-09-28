"""Legacy untagged VC-1: SDR unlock, retained HDR safeguards and VA-API command."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.models.probe import legacy_vc1_sdr_colours
from app.services.encoding_capability import CPUEncodeCapability, GPUEncodeCapability
from app.services.ffprobe import parse_ffprobe
from app.services.policy import PolicyEngine
from app.workers import vaapi
from app.workers.ffmpeg import FFmpegEncoder
from tests.test_real_encoding import build_real_app, movie_item, movie_preset, qsv_preset, queue_job

COLOUR_KEYS = ('color_primaries', 'color_transfer', 'color_space')
HDR_REASONS = ('HDR signalling is unsupported', 'Dynamic HDR metadata', 'confirmed SDR')


def payload(**video):
    raw = json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text())
    stream = raw['streams'][0]
    for key in COLOUR_KEYS:
        del stream[key]
    stream.update(codec_name='vc1', profile='Advanced')
    stream.update(video)
    return raw


def probe(**video):
    return parse_ffprobe(payload(**video))


def with_side_data(side):
    raw = payload()
    raw['streams'][0]['side_data_list'] = [side]
    return parse_ffprobe(raw)


def hdr_reasons(messages):
    return [message for message in messages if any(reason in message for reason in HDR_REASONS)]


def policy_reasons(facts):
    item = movie_item(facts, video_codec=facts.streams[0].codec)
    return PolicyEngine().evaluate(item=item, scope='movie', preset=movie_preset(), effective_tags=[],
                                   preserve_audio=True, preserve_subtitles=True).reasons


@pytest.mark.parametrize('width,height,expected', [
    (1920, 1080, ('bt709', 'bt709', 'bt709')),
    (1280, 720, ('bt709', 'bt709', 'bt709')),
    (720, 576, ('bt470bg', 'smpte170m', 'bt470bg')),
    (720, 480, ('smpte170m', 'smpte170m', 'smpte170m')),
])
def test_untagged_progressive_8bit_vc1_is_assumed_sdr(width, height, expected):
    facts = probe(width=width, height=height)
    assert legacy_vc1_sdr_colours(facts.streams[0]) == expected
    assert legacy_vc1_sdr_colours(probe(color_transfer='unknown').streams[0]) is not None


@pytest.mark.parametrize('facts', [
    pytest.param(lambda: probe(codec_name='hevc', profile='Main 10'), id='hevc'),
    pytest.param(lambda: probe(codec_name='av1', profile='Main'), id='av1'),
    pytest.param(lambda: probe(codec_name='h264', profile='High'), id='h264'),
    pytest.param(lambda: probe(field_order='tt'), id='interlaced'),
    pytest.param(lambda: probe(field_order='unknown'), id='unknown-scan'),
    pytest.param(lambda: probe(pix_fmt='yuv420p10le'), id='10-bit'),
    pytest.param(lambda: probe(bits_per_raw_sample='10'), id='10-bit-raw-sample'),
    pytest.param(lambda: probe(color_transfer='smpte2084'), id='pq'),
    pytest.param(lambda: probe(color_transfer='arib-std-b67'), id='hlg'),
    pytest.param(lambda: probe(color_primaries='bt2020'), id='bt2020-primaries'),
    pytest.param(lambda: probe(color_space='bt2020nc'), id='bt2020-matrix'),
    pytest.param(lambda: with_side_data({'side_data_type': 'Mastering display metadata'}), id='mastering'),
    pytest.param(lambda: with_side_data({'side_data_type': 'Content light level metadata'}), id='cll'),
    pytest.param(lambda: with_side_data({'side_data_type': 'DOVI configuration record'}), id='dv'),
    pytest.param(lambda: with_side_data({'side_data_type': 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'}),
                 id='hdr10plus'),
])
def test_no_unlock_without_legacy_vc1_facts_or_with_hdr_evidence(facts):
    facts = facts()
    assert legacy_vc1_sdr_colours(facts.streams[0]) is None
    assert hdr_reasons(policy_reasons(facts)) or facts.streams[0].scan_type != 'progressive'
    item = movie_item(facts, video_codec=facts.streams[0].codec)
    assert any('confirmed SDR' in reason or 'progressive' in reason
               for reason in CPUEncodeCapability().reasons(item, queue_job()))


# The VC-1 rule is codec-specific. Untagged H.264 (common in Blu-ray remuxes) and HEVC
# stay uncertain until each gets its own explicit decision.
UNTAGGED_OTHER_CODECS = pytest.mark.parametrize('codec,profile', [('hevc', 'Main'), ('h264', 'High')])


@UNTAGGED_OTHER_CODECS
def test_untagged_non_vc1_remains_blocked_by_policy_and_capability(codec, profile):
    facts = probe(codec_name=codec, profile=profile)
    assert legacy_vc1_sdr_colours(facts.streams[0]) is None
    assert facts.streams[0].hdr is not None and facts.streams[0].hdr.classify().uncertain
    assert hdr_reasons(policy_reasons(facts))
    item = movie_item(facts, video_codec=codec)
    job = queue_job(qsv_preset(), backend='qsv')
    assert any('confirmed SDR' in reason for reason in GPUEncodeCapability().reasons(item, job))
    assert any('confirmed SDR' in reason for reason in CPUEncodeCapability().reasons(item, queue_job()))


def test_legacy_vc1_passes_policy_and_both_execution_capabilities():
    facts = probe()
    assert not hdr_reasons(policy_reasons(facts))
    item = movie_item(facts, video_codec='vc1')
    assert CPUEncodeCapability().reasons(item, queue_job()) == []
    assert GPUEncodeCapability().reasons(item, queue_job(qsv_preset(), backend='qsv')) == []


def test_filesystem_library_projects_legacy_vc1_as_sdr_and_eligible(tmp_path, monkeypatch):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe())
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        assert movie.video_codec == 'vc1' and movie.hdr is None
        result = processor.evaluate_compression(movie.id, 'movie', 'movie-streaming-quality')
        assert result.eligible is True, result.reasons


@UNTAGGED_OTHER_CODECS
def test_filesystem_library_keeps_untagged_non_vc1_unknown_and_blocked(tmp_path, monkeypatch, codec, profile):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe(codec_name=codec, profile=profile))
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        assert movie.hdr == 'unknown'
        result = processor.evaluate_compression(movie.id, 'movie', 'movie-streaming-quality')
        assert result.eligible is False
        assert hdr_reasons(result.reasons)


@pytest.mark.parametrize('profile,hardware', [('Advanced', False), ('Main', True), ('Simple', True)])
def test_vc1_advanced_is_not_selected_for_vaapi_decode(profile, hardware):
    assert vaapi.hardware_decode(probe(profile=profile).streams[0]) is hardware


def test_vc1_advanced_gpu_command_uses_software_decode_upload_and_hevc_vaapi(tmp_path):
    facts = probe()
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(qsv_preset(), backend='qsv'),
        Path('/source.mkv'), tmp_path / 'out.mkv', facts, Path('/dev/dri/renderD128'))
    assert command[command.index('-init_hw_device') + 1] == 'vaapi=va:/dev/dri/renderD128'
    assert command[command.index('-filter_hw_device') + 1] == 'va'
    assert not [flag for flag in command if flag.startswith(('-hwaccel', '-hwaccel_device',
                                                             '-hwaccel_output_format'))]
    # The SDR assumption is stamped on the frames: newer ffmpeg lets frame colour
    # properties override -color_* options, which left real output untagged.
    assert command[command.index('-filter:v:0') + 1] == (
        'format=p010le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,hwupload')
    assert not any('scale_vaapi' in argument for argument in command)
    assert command[command.index('-c:v:0') + 1] == 'hevc_vaapi'
    assert command[command.index('-profile:v:0') + 1] == 'main10'
    assert command[command.index('-rc_mode:v:0') + 1] == 'QVBR'
    assert command[command.index('-color_primaries:v:0') + 1] == 'bt709'
    assert command[command.index('-color_trc:v:0') + 1] == 'bt709'
    assert command[command.index('-colorspace:v:0') + 1] == 'bt709'
    assert command[command.index('-color_range:v:0') + 1] == 'tv'
    assert command.index('-init_hw_device') < command.index('-i') < command.index('-filter:v:0')


@pytest.mark.parametrize('width,height,filters', [
    (1920, 1080, 'format=yuv420p10le,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709'),
    (720, 576, 'format=yuv420p10le,setparams=color_primaries=bt470bg:color_trc=smpte170m:colorspace=bt470bg'),
])
def test_legacy_vc1_cpu_command_stamps_assumed_sdr_on_frames(tmp_path, width, height, filters):
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(), Path('/source.mkv'),
        tmp_path / 'out.mkv', probe(width=width, height=height))
    assert command[command.index('-c:v:0') + 1] == 'libx265'
    assert command[command.index('-filter:v:0') + 1] == filters
    assert command[command.index('-pix_fmt:v:0') + 1] == 'yuv420p10le'


def test_vc1_hardware_decode_path_stamps_after_scale_vaapi(tmp_path):
    command = FFmpegEncoder.build_command('ffmpeg', queue_job(qsv_preset(), backend='qsv'),
        Path('/source.mkv'), tmp_path / 'out.mkv', probe(profile='Main'), Path('/dev/dri/renderD128'))
    assert '-hwaccel:0' in command
    assert command[command.index('-filter:v:0') + 1] == (
        'scale_vaapi=format=p010,setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709')


@UNTAGGED_OTHER_CODECS
@pytest.mark.parametrize('backend', ['cpu', 'qsv'])
def test_untagged_non_vc1_command_writes_no_invented_colour_tags(tmp_path, codec, profile, backend):
    facts = probe(codec_name=codec, profile=profile)
    job = queue_job(qsv_preset(), backend='qsv') if backend == 'qsv' else queue_job()
    command = FFmpegEncoder.build_command('ffmpeg', job, Path('/source.mkv'), tmp_path / 'out.mkv',
                                          facts, Path('/dev/dri/renderD128'))
    assert not {'-color_primaries:v:0', '-color_trc:v:0', '-colorspace:v:0'} & set(command)
    assert not any('setparams' in argument for argument in command)


@pytest.mark.parametrize('backend', ['cpu', 'qsv'])
def test_tagged_source_command_is_unchanged_by_frame_stamping(tmp_path, backend):
    facts = parse_ffprobe(json.loads((PROJECT_ROOT / 'fixtures/ffprobe/progressive.json').read_text()))
    job = queue_job(qsv_preset(), backend='qsv') if backend == 'qsv' else queue_job()
    command = FFmpegEncoder.build_command('ffmpeg', job, Path('/source.mkv'), tmp_path / 'out.mkv',
                                          facts, Path('/dev/dri/renderD128'))
    assert not any('setparams' in argument for argument in command)
    assert ('-filter:v:0' in command) is (backend == 'qsv')
    assert command[command.index('-color_primaries:v:0') + 1] == 'bt709'


def test_legacy_vc1_output_validates_only_with_written_sdr_tags(tmp_path):
    source = probe()
    item = movie_item(source, video_codec='vc1')
    job = queue_job(qsv_preset(), backend='qsv')
    output = source.model_copy(deep=True)
    video = output.streams[0]
    video.codec, video.profile, video.pixel_format = 'hevc', 'Main 10', 'yuv420p10le'
    assert video.hdr is not None
    video.hdr = video.hdr.model_copy(update={'bit_depth': 10, 'primaries': 'bt709',
                                             'transfer': 'bt709', 'matrix': 'bt709'})
    path = tmp_path / 'output.mkv'
    path.write_bytes(b'fixture')
    guard = GPUEncodeCapability()
    assert guard.validate_output(item, job, output, path) == []
    video.hdr = video.hdr.model_copy(update={'primaries': None, 'transfer': None, 'matrix': None})
    assert any('confirmed SDR' in error for error in guard.validate_output(item, job, output, path))
