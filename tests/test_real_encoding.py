import io
import json
import subprocess
from typing import cast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.container import build_media_processor
from app.main import create_app
from app.models.inventory import SourceReference
from app.models.media import AudioTrack, Movie
from app.models.preset import CompressionPreset
from app.models.preferences import WorkerLaneSettings, WorkerSettings
from app.models.probe import HDRSignalling, MediaProbeResult, StreamFacts
from app.models.queue import EnqueueRequest, QueueJob
from app.repositories.preset_seed import SeedPresetRepository
from app.services.encoding_capability import CPUEncodeCapability, GPUEncodeCapability
from app.services.errors import Conflict
from app.services.ffprobe import FFprobeService
from app.workers.ffmpeg import FFmpegEncoder, FFmpegError
from app.workers.real import RealEncoderWorker


@pytest.fixture
def probe_facts():
    payload = json.loads((PROJECT_ROOT / "fixtures/ffprobe/progressive.json").read_text())
    from app.services.ffprobe import parse_ffprobe
    return parse_ffprobe(payload)


def movie_preset() -> CompressionPreset:
    presets = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    return next(preset for preset in presets if preset.id == "movie-streaming-quality")


def gpu_preset(efficient_audio: bool = False) -> CompressionPreset:
    presets = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    preset_id = "show-streaming-efficient-audio" if efficient_audio else "show-streaming-quality"
    return next(preset for preset in presets if preset.id == preset_id)


def queue_job(preset=None, **changes) -> QueueJob:
    preset = preset or movie_preset()
    values = dict(
        id="job-test-1", media_id="file-test-1", scope="movie", name="Fixture movie",
        backend="cpu", preset=preset, preserve_audio=True, requested_preserve_audio=True,
        preserve_subtitles=True, source_size=400_000_000, estimated_output_size=None,
        estimated_saving=None, source_codec="h264", execution_mode="real",
        source_file_id="file-test-1", source_revision_id="revision-1",
        source_reference=SourceReference(file_id="file-test-1", revision_id="revision-1",
            captured=True, root_id="movie-root", relative_path="Fixture.mkv", filesystem_id="dev-1",
            inode=101, generation="gen-1", size=400_000_000, mtime_ns=100,
            ctime_ns=100, hardlinks=1),
        replace_source=False, created_at="2026-01-01T00:00:00+00:00",
    )
    values.update(changes)
    return QueueJob.model_validate(values)


def movie_item(probe, **changes) -> Movie:
    values = dict(id="file-test-1", media_id="logical-test-1", revision_id="revision-1",
        processing_supported=True, probe=probe, name="Fixture movie", year=1990,
        path="/readonly/Fixture.mkv", source=None, width=1920, height=1080,
        resolution="1080p", video_codec="h264", video_bitrate=12_000_000,
        duration_seconds=120.5, hdr=None, interlaced=False, size=400_000_000,
        audio=[], hardlinks=1)
    values.update(changes)
    return Movie.model_validate(values)


def test_runtime_capability_checks_ffmpeg_ffprobe_workspace_and_x265(tmp_path, monkeypatch):
    from app.services import encoding_runtime
    workspace = tmp_path / "workspace"
    (workspace / "jobs").mkdir(parents=True)
    movie_root = tmp_path / "movies"
    movie_root.mkdir()
    monkeypatch.setattr(encoding_runtime, "executable", lambda binary: f"/fake/{binary}")
    monkeypatch.setattr(encoding_runtime.FFprobeService, "runtime_check", lambda binary: (True, None))
    monkeypatch.setattr(encoding_runtime.FFmpegEncoder, "runtime_check", lambda binary: (True, None))
    monkeypatch.setattr(encoding_runtime.FFmpegEncoder, "vaapi_runtime_check", lambda binary, device, ffprobe, workspace: (True, None))
    status = encoding_runtime.capability_status("ffmpeg", "ffprobe", workspace, {"movie": movie_root})
    assert status["available"] and status["ffprobe_available"] and status["libx265_available"]
    assert status["hevc_vaapi_available"] and status["supported_backends"] == ["cpu", "gpu"]
    assert status["workspace_writable"]


def test_cpu_worker_runtime_probe_does_not_touch_gpu_device(tmp_path, monkeypatch):
    from app.services import encoding_runtime
    workspace = tmp_path / "workspace"
    (workspace / "jobs").mkdir(parents=True)
    movie_root = tmp_path / "movies"
    movie_root.mkdir()
    monkeypatch.setattr(encoding_runtime, "executable", lambda binary: f"/fake/{binary}")
    monkeypatch.setattr(encoding_runtime.FFprobeService, "runtime_check", lambda binary: (True, None))
    monkeypatch.setattr(encoding_runtime.FFmpegEncoder, "runtime_check", lambda binary: (True, None))

    def unexpected_gpu(*args):
        raise AssertionError("CPU worker must not probe GPU hardware")

    monkeypatch.setattr(encoding_runtime.FFmpegEncoder, "vaapi_runtime_check", unexpected_gpu)
    status = encoding_runtime.capability_status(
        "ffmpeg", "ffprobe", workspace, {"movie": movie_root},
        Path("/dev/dri/renderD128"), frozenset({"cpu"}),
    )
    assert status["supported_backends"] == ["cpu"]
    assert status["cpu_available"] is True
    assert status["hevc_vaapi_available"] is None


def test_tool_runtime_checks_accept_diagnostics_on_stderr(monkeypatch):
    import subprocess

    from app.workers import ffmpeg

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr=" V....D libx265")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="ffprobe version 7.0")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    assert FFmpegEncoder.runtime_check("ffmpeg") == (True, None)
    assert FFprobeService.runtime_check("ffprobe") == (True, None)


def test_vaapi_runtime_check_requires_encoder_device_and_smoke_test(tmp_path, monkeypatch):
    from app.workers import ffmpeg
    device = tmp_path / "renderD128"
    device.touch()
    (tmp_path / "jobs").mkdir()
    monkeypatch.setattr(ffmpeg.stat, "S_ISCHR", lambda mode: True)
    monkeypatch.setattr(ffmpeg.os, "access", lambda path, mode: True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, stdout=" V....D hevc_vaapi", stderr="")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"streams": [{
                "codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le", "nb_read_frames": "10"
            }]}), stderr="")
        Path(command[-1]).write_bytes(b"smoke")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    assert FFmpegEncoder.vaapi_runtime_check("ffmpeg", device, "ffprobe", tmp_path) == (True, None)
    assert any("format=p010le,hwupload" in command and "QVBR" in command and "main10" in command for command in calls)
    assert not list((tmp_path / "jobs").iterdir())


def test_command_maps_streams_and_applies_only_supported_crf_settings(probe_facts, tmp_path):
    job = queue_job()
    partial = tmp_path / "Fixture.kompressor.partial.mkv"
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/read-only/Film.mkv"), partial, probe_facts)
    assert command[:2] == ["/usr/bin/ffmpeg", "-hide_banner"]
    assert "-progress" in command and command[command.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in command
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
    assert command[command.index("-c:v:0") + 1] == "libx265"
    assert command[command.index("-crf:v:0") + 1] == str(job.preset.quality_value)
    assert command[command.index("-preset:v:0") + 1] == job.preset.encoder_preset
    assert command[command.index("-pix_fmt:v:0") + 1] == "yuv420p10le"
    assert command[command.index("-color_primaries:v:0") + 1] == "bt709"
    assert command[command.index("-color_trc:v:0") + 1] == "bt709"
    assert command[command.index("-colorspace:v:0") + 1] == "bt709"
    assert command[command.index("-color_range:v:0") + 1] == "tv"
    mapped = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-map"]
    assert mapped == [f"0:{stream.index}" for stream in probe_facts.streams]
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    assert command[command.index("-map_metadata") + 1] == "0"
    assert command[command.index("-map_chapters") + 1] == "0"
    assert command[-1] == str(partial)


def test_gpu_command_uses_render_device_qvbr_and_10bit_output(probe_facts, tmp_path):
    preset = gpu_preset()
    job = queue_job(preset=preset, backend="gpu", scope="show")
    partial = tmp_path / "Fixture.kompressor.partial.mkv"
    device = Path("/dev/dri/renderD128")
    command = FFmpegEncoder.build_command(
        "/usr/bin/ffmpeg", job, Path("/read-only/Episode.mkv"), partial, probe_facts,
        gpu_device=device,
    )
    assert command[command.index("-init_hw_device") + 1] == f"vaapi=va:{device}"
    assert command[command.index("-c:v:0") + 1] == "hevc_vaapi"
    assert command[command.index("-global_quality:v:0") + 1] == "23"
    assert "-preset:v:0" not in command
    assert command[command.index("-rc_mode:v:0") + 1] == "QVBR"
    assert command[command.index("-b:v:0") + 1] == "4000000"
    assert command[command.index("-profile:v:0") + 1] == "main10"
    assert command[command.index("-filter:v:0") + 1] == "format=p010le,hwupload"
    assert command[command.index("-color_primaries:v:0") + 1] == "bt709"
    assert command[command.index("-color_trc:v:0") + 1] == "bt709"
    assert command[command.index("-colorspace:v:0") + 1] == "bt709"
    assert command[command.index("-color_range:v:0") + 1] == "tv"
    assert "-crf:v:0" not in command
    assert command[-1] == str(partial)


def test_gpu_efficient_audio_converts_only_tracks_that_need_it(probe_facts, tmp_path):
    preset = gpu_preset(efficient_audio=True)
    job = queue_job(preset=preset, backend="gpu", scope="show", preserve_audio=False,
                    requested_preserve_audio=False)
    partial = tmp_path / "Fixture.kompressor.partial.mkv"
    command = FFmpegEncoder.build_command(
        "/usr/bin/ffmpeg", job, Path("/read-only/Episode.mkv"), partial, probe_facts,
        gpu_device=Path("/dev/dri/renderD128"),
    )
    assert command[command.index("-c:a:0") + 1] == "eac3"
    assert command[command.index("-b:a:0") + 1] == str(preset.target_audio_bitrate)
    assert command[command.index("-ac:a:0") + 1] == "6"
    # The AAC stereo commentary has unknown bitrate and is copied by the preset rules.
    assert "-c:a:1" not in command


MKVMERGE_STATS = {"BPS-eng": "18360077", "DURATION-eng": "00:42:05.482000000",
                  "NUMBER_OF_FRAMES-eng": "60551", "NUMBER_OF_BYTES-eng": "5796005604",
                  "_STATISTICS_WRITING_APP-eng": "mkvmerge v21.0.0", "_STATISTICS_WRITING_DATE_UTC-eng":
                  "2018-03-17 23:42:33", "_STATISTICS_TAGS-eng": "BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES"}


def metadata_args(command, specifier):
    flag = f"-metadata:s:{specifier}"
    return {command[i + 1] for i, value in enumerate(command) if value == flag}


def with_statistics(probe):
    return probe.model_copy(update={"streams": [
        stream.model_copy(update={"metadata": {**stream.metadata, **MKVMERGE_STATS, "BPS": "1",
                                               "title": "Keep me"}})
        if stream.kind in {"video", "audio"} else stream for stream in probe.streams]})


@pytest.mark.parametrize("backend", ["cpu", "gpu"])
def test_reencoded_video_drops_stale_mkvmerge_statistics(probe_facts, tmp_path, backend):
    job = queue_job(gpu_preset(), backend="gpu") if backend == "gpu" else queue_job()
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/read-only/Film.mkv"),
        tmp_path / "out.mkv", with_statistics(probe_facts), gpu_device=Path("/dev/dri/renderD128"))
    assert metadata_args(command, "v:0") == {f"{key}=" for key in MKVMERGE_STATS} | {"BPS="}
    # Copied audio keeps its statistics; they still describe the copied bitstream.
    assert not metadata_args(command, "a:0") and not metadata_args(command, "a:1")
    assert not any("title" in argument for argument in command if argument.endswith("="))
    assert command.index("-metadata:s:v:0") > command.index("-map_metadata")


def test_converted_audio_drops_statistics_but_copied_audio_keeps_them(probe_facts, tmp_path):
    job = queue_job(preset=gpu_preset(efficient_audio=True), backend="gpu", scope="show",
                    preserve_audio=False, requested_preserve_audio=False)
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/read-only/Episode.mkv"),
        tmp_path / "out.mkv", with_statistics(probe_facts), gpu_device=Path("/dev/dri/renderD128"))
    assert command[command.index("-c:a:0") + 1] == "eac3"
    assert "BPS-eng=" in metadata_args(command, "a:0")
    assert "-c:a:1" not in command and not metadata_args(command, "a:1")


def test_sources_without_statistics_get_no_metadata_edits(probe_facts, tmp_path):
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", queue_job(), Path("/read-only/Film.mkv"),
                                          tmp_path / "out.mkv", probe_facts)
    assert not any(argument.startswith("-metadata") for argument in command)


def test_command_converts_mov_text_subtitle_for_matroska(probe_facts, tmp_path):
    streams = [
        stream.model_copy(update={"codec": "mov_text"}) if stream.kind == "subtitle" and stream.index == 3 else stream
        for stream in probe_facts.streams
    ]
    probe = probe_facts.model_copy(update={"streams": streams})
    job = queue_job()
    partial = tmp_path / "Fixture.kompressor.partial.mkv"
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/read-only/Film.mp4"), partial, probe)
    assert command[command.index("-c:s:0") + 1] == "srt"
    assert "-c:s:1" not in command
    assert command[command.index("-c") + 1] == "copy"


def test_execution_capability_rejects_unsupported_modes(probe_facts):
    guard = CPUEncodeCapability()
    item = movie_item(probe_facts)
    job = queue_job()
    assert not guard.reasons(item, job)
    variants = [
        (item, job.model_copy(update={"backend": "gpu"}), "CPU backend only"),
        (item.model_copy(update={"audio": [AudioTrack(codec="dts", channels=6, bitrate=1_500_000)]}),
            job.model_copy(update={"preserve_audio": False}), "audio conversion safely"),
        (item, job.model_copy(update={"preset": job.preset.model_copy(update={"target_resolution": "max_720p"})}), "source resolution only"),
        (item.model_copy(update={"hardlinks": 2}), job, "one known source hardlink"),
        (item.model_copy(update={"interlaced": True}), job, "progressive video"),
        (item.model_copy(update={"probe": probe_facts.model_copy(update={"streams": [
            stream.model_copy(update={"scan_type": "unknown"}) if stream.kind == "video" else stream
            for stream in probe_facts.streams]})}), job, "progressive video"),
        (item.model_copy(update={"probe": probe_facts.model_copy(update={"streams": [
            stream.model_copy(update={"hdr": HDRSignalling(transfer="smpte2084", primaries="bt2020",
                matrix="bt2020nc", bit_depth=10, inspection_complete=True)}) if stream.kind == "video" else stream
            for stream in probe_facts.streams]})}), job, "confirmed SDR"),
        (item.model_copy(update={"probe": probe_facts.model_copy(update={"streams": [
            stream.model_copy(update={"hdr": HDRSignalling()}) if stream.kind == "video" else stream
            for stream in probe_facts.streams]})}), job, "confirmed SDR"),
        (item.model_copy(update={"hardlinks": None}), job, "one known source hardlink"),
    ]
    for candidate, candidate_job, reason in variants:
        assert any(reason in message for message in guard.reasons(candidate, candidate_job)), (reason, guard.reasons(candidate, candidate_job))


def test_output_validation_rejects_changed_sdr_colour_signalling(probe_facts, tmp_path):
    guard = CPUEncodeCapability()
    job = queue_job()
    item = movie_item(probe_facts)
    output_file = tmp_path / "colour-shift.mkv"
    output_file.write_bytes(b"fake")
    streams = [
        stream.model_copy(update={
            "codec": "hevc",
            "pixel_format": "yuv420p10le",
            "hdr": stream.hdr.model_copy(update={
                "bit_depth": 10,
                "primaries": "smpte170m",
                "transfer": "smpte170m",
                "matrix": "smpte170m",
            }),
        }) if stream.kind == "video" else stream
        for stream in probe_facts.streams
    ]
    shifted = probe_facts.model_copy(update={"streams": streams})
    errors = guard.validate_output(item, job, shifted, output_file)
    assert any("colour primaries" in error for error in errors)
    assert any("transfer characteristics" in error for error in errors)
    assert any("matrix coefficients" in error for error in errors)


def test_gpu_efficient_audio_output_validation_accepts_planned_codecs(
        probe_facts, tmp_path):
    guard = GPUEncodeCapability()
    source_audio = [
        AudioTrack(codec=stream.codec, channels=stream.channels, bitrate=stream.bitrate,
                   language=stream.language, title=stream.title, stream_index=stream.index,
                   channel_layout=stream.channel_layout, dispositions=stream.dispositions)
        for stream in probe_facts.streams if stream.kind == "audio"
    ]
    item = movie_item(probe_facts, audio=source_audio)
    job = queue_job(
        preset=gpu_preset(efficient_audio=True), backend="gpu", scope="show",
        preserve_audio=False, requested_preserve_audio=False,
    )
    output_streams = []
    audio_index = 0
    for stream in probe_facts.streams:
        if stream.kind == "video":
            output_streams.append(stream.model_copy(update={
                "codec": "hevc", "pixel_format": "yuv420p10le", "profile": "Main 10",
                "hdr": stream.hdr.model_copy(update={"bit_depth": 10}),
            }))
        elif stream.kind == "audio":
            if audio_index == 0:
                output_streams.append(stream.model_copy(update={"codec": "eac3", "channels": 6}))
            else:
                output_streams.append(stream)
            audio_index += 1
        else:
            output_streams.append(stream)
    output_probe = probe_facts.model_copy(update={"streams": output_streams})
    output_file = tmp_path / "gpu-output.mkv"
    output_file.write_bytes(b"validated")
    assert guard.validate_output(item, job, output_probe, output_file) == []


def test_gpu_capability_accepts_icq_and_efficient_audio(probe_facts):
    guard = GPUEncodeCapability()
    audio = [
        AudioTrack(codec=stream.codec, channels=stream.channels, bitrate=stream.bitrate,
                   language=stream.language, title=stream.title, stream_index=stream.index,
                   channel_layout=stream.channel_layout, dispositions=stream.dispositions)
        for stream in probe_facts.streams if stream.kind == "audio"
    ]
    item = movie_item(probe_facts, audio=audio)
    job = queue_job(preset=gpu_preset(efficient_audio=True), backend="gpu", scope="show",
                    preserve_audio=False, requested_preserve_audio=False)
    assert not guard.reasons(item, job)
    wrong_backend = job.model_copy(update={"backend": "cpu"})
    assert any("GPU backend only" in reason for reason in guard.reasons(item, wrong_backend))


def test_output_validation_reports_codec_resolution_duration_and_stream_loss(probe_facts, tmp_path):
    guard = CPUEncodeCapability()
    job = queue_job()
    item = movie_item(probe_facts)
    output_file = tmp_path / "partial.mkv"
    output_file.write_bytes(b"fake")
    changed_streams = [
        stream.model_copy(update={"codec": "h264", "width": 1280, "scan_type": "interlaced"})
        if stream.kind == "video" else stream
        for stream in probe_facts.streams if stream.kind != "attachment"
    ]
    invalid = probe_facts.model_copy(update={"streams": changed_streams, "duration_seconds": 10})
    errors = guard.validate_output(item, job, invalid, output_file)
    assert any("expected HEVC" in error for error in errors)
    assert any("resolution" in error for error in errors)
    assert any("scan type" in error for error in errors)
    assert any("duration" in error for error in errors)
    assert any("attachment streams" in error for error in errors)


class FakeProcess:
    def __init__(self, command, *, exit_code=0, output_size=100_000_000, stderr=""):
        self.command = command
        self.stdout = io.StringIO("frame=1440\nout_time_us=60000000\nprogress=continue\n"
                                  "frame=2892\nout_time_us=120500000\nprogress=end\n")
        self.stderr = io.StringIO(stderr)
        self.returncode = exit_code
        # Output to stdout ("-") means an unpatched helper such as audio hashing.
        assert command[-1] != "-", "FakeProcess only simulates encodes that write a file"
        Path(command[-1]).touch()
        with Path(command[-1]).open("wb") as output:
            output.truncate(output_size)
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def build_real_app(tmp_path, monkeypatch, source_probe, output_probe=None, *, exit_code=0,
                   start_workers=False, runtime_available=True, source_name="Fixture.mkv",
                   output_size=100_000_000, replaced_probe=None):
    root = tmp_path / "movies"
    root.mkdir()
    source = root / source_name
    with source.open("wb") as handle:
        handle.truncate(400_000_000)
    workspace = tmp_path / "workspace"
    (workspace / "jobs").mkdir(parents=True)
    config = Settings(media_backend="filesystem", movies_root=root,
        database_path=tmp_path / "state.sqlite3", workspace_root=workspace,
        ffmpeg_binary="/fake/ffmpeg", ffprobe_binary="/fake/ffprobe")
    from app import container
    monkeypatch.setattr(container, "capability_status", lambda *args: {
        "available": runtime_available, "ffmpeg_available": runtime_available,
        "ffprobe_available": runtime_available,
        "libx265_available": runtime_available, "ffmpeg_binary": "/fake/ffmpeg",
        "ffprobe_binary": "/fake/ffprobe", "workspace_root": str(workspace),
        "workspace_writable": True, "unavailable_reason": None,
    })

    def inspect(self, path):
        if ".kompressor" in path.name and output_probe:  # partial or kept workspace output
            return output_probe
        # After an in-place replacement the source path holds the (smaller) output.
        if path == source and output_probe and path.stat().st_size == output_size:
            return replaced_probe or output_probe
        return source_probe
    monkeypatch.setattr(FFprobeService, "inspect", inspect)
    hashed = []

    def packet_hashes(self, job_id, path, selectors):
        # Copied tracks hash identically unless a test says otherwise.
        hashed.append((Path(path), list(selectors)))
        return [f"track-{ordinal}" for ordinal in range(len(selectors))]
    monkeypatch.setattr(FFmpegEncoder, "packet_hashes", packet_hashes)
    # Outputs decode cleanly unless a test says otherwise.
    monkeypatch.setattr(FFmpegEncoder, "decode_errors", lambda self, job_id, path, **frames: [])
    spawned = []
    def popen(command, **kwargs):
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        process = FakeProcess(command, exit_code=exit_code, output_size=output_size,
                              stderr="fixture ffmpeg error" if exit_code else "")
        spawned.append(process)
        return process
    monkeypatch.setattr("app.workers.ffmpeg.subprocess.Popen", popen)
    application = create_app(config=config, start_workers=start_workers,
                             start_real_worker=start_workers)
    return application, source, workspace, spawned


def test_real_worker_validates_promotes_and_measures_output(tmp_path, monkeypatch, probe_facts):
    output_streams = [stream.model_copy(update={"codec": "hevc", "pixel_format": "yuv420p10le", "hdr": stream.hdr.model_copy(update={"bit_depth": 10})}) if stream.kind == "video" else stream
                      for stream in probe_facts.streams]
    output_probe = probe_facts.model_copy(update={"streams": output_streams})
    application, source_path, workspace, spawned = build_real_app(
        tmp_path, monkeypatch, probe_facts, output_probe)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        before = source_path.stat()
        result = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
                                                       preset_id="movie-streaming-quality"))
        assert len(result["added"]) == 1
        job_id = result["added"][0]["id"]
        worker = processor.queue.worker
        states = ["queued"]
        job = processor.queue.claim_next("cpu")
        assert job is not None and job.status == "encoding"
        states.append(job.status)
        original_validating = processor.queue.set_real_validating
        def validating(job_id, path):
            changed = original_validating(job_id, path)
            states.append(processor.queue._find(job_id).status)
            return changed
        processor.queue.set_real_validating = validating
        original_complete = processor.queue.complete_real_job
        def complete(job_id, path, size):
            changed = original_complete(job_id, path, size)
            states.append(processor.queue._find(job_id).status)
            return changed
        processor.queue.complete_real_job = complete
        checks = []
        original_guard = worker.guard.before_processing
        def guard(reference):
            checks.append("guard")
            return original_guard(reference)
        worker.guard.before_processing = guard
        original_popen = __import__("app.workers.ffmpeg", fromlist=["subprocess"]).subprocess.Popen
        def popen(command, **kwargs):
            checks.append("popen")
            return original_popen(command, **kwargs)
        monkeypatch.setattr("app.workers.ffmpeg.subprocess.Popen", popen)
        worker._execute(job)
        saved = next(item for item in processor.get_queue()["history"] if item["id"] == job_id)
        assert saved["status"] == "completed"
        assert states == ["queued", "encoding", "validating", "completed"]
        assert saved["measured_saving"] == 300_000_000
        assert saved["output_size"] == 100_000_000
        assert saved["estimated_saving"] is None  # CRF planning estimates remain separate.
        assert Path(saved["output_path"]).is_relative_to(workspace / "jobs" / job_id)
        assert Path(saved["output_path"]).name == "Fixture.kompressor.mkv"
        assert not Path(saved["output_path"].replace(".kompressor.mkv", ".kompressor.partial.mkv")).exists()
        assert checks[:2] == ["guard", "popen"]
        after = source_path.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)


def test_measured_saving_is_negative_when_output_is_larger(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        assert processor.queue.set_real_validating(job.id, "/tmp/larger-output.mkv")
        processor.queue.complete_real_job(job.id, "/tmp/larger-output.mkv", 500_000_000)
        saved = next(item for item in processor.get_queue()["history"] if item["id"] == added["id"])
        assert saved["measured_saving"] == -100_000_000


def test_snapshot_repairs_legacy_clamped_measured_saving(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        assert processor.queue.set_real_validating(job.id, "/tmp/larger-output.mkv")
        assert processor.queue.complete_real_job(job.id, "/tmp/larger-output.mkv", 500_000_000)
        legacy = processor.queue._find(added["id"])
        legacy.measured_saving = 0
        processor.queue.repository.save(legacy)
        exposed = next(item for item in processor.get_queue()["history"] if item["id"] == added["id"])
        assert exposed["measured_saving"] == -100_000_000


def test_completion_refuses_persisted_stop_request(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        assert processor.queue.set_real_validating(job.id, "/tmp/race-output.mkv")
        processor.stop_job(job.id)
        assert processor.queue.complete_real_job(job.id, "/tmp/race-output.mkv", 100_000_000) is False
        persisted = processor.queue._find(added["id"])
        assert persisted.status == "stopping"
        assert persisted.cancel_requested is True


def test_ffmpeg_failure_is_persisted_and_partial_is_removed(tmp_path, monkeypatch, probe_facts):
    application, source_path, workspace, spawned = build_real_app(
        tmp_path, monkeypatch, probe_facts, exit_code=7)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        result = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
                                                       preset_id="movie-streaming-quality"))
        job_id = result["added"][0]["id"]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        worker = processor.queue.worker
        with pytest.raises(FFmpegError):
            worker._execute(job)
        processor.queue.fail_real_job(job_id, "fixture ffmpeg error", 7)
        worker.encoder.cleanup(job_id, remove_final=True)
        saved = next(item for item in processor.get_queue()["history"] if item["id"] == job_id)
        assert saved["status"] == "failed"
        assert saved["error_message"] == "fixture ffmpeg error"
        assert saved["ffmpeg_exit_code"] == 7
        assert saved["measured_saving"] is None
        assert not list((workspace / "jobs" / job_id).glob("*.partial.mkv"))
        assert source_path.stat().st_size == 400_000_000


@pytest.mark.parametrize("progress, expected", [(50.0, "stop"), (50.1, "finish")])
def test_real_quiet_start_uses_strict_progress_cutoff(tmp_path, monkeypatch, probe_facts,
                                                     progress, expected):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(
            media_ids=[movie.id], scope="movie", preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        processor.queue.update_real_progress(job.id, progress, 10)
        processor.queue.controls.save(WorkerSettings(
            timezone="UTC",
            cpu=WorkerLaneSettings(quiet_hours_enabled=True, quiet_start="00:00",
                                   quiet_end="00:00", quiet_cutoff_percent=50),
            gpu=WorkerLaneSettings(),
        ))
        assert processor.queue.enforce_quiet_start("cpu") == expected
        saved = processor.queue._find(added["id"])
        if expected == "stop":
            assert saved.status == "stopping"
            assert saved.cancel_requested is True
            assert saved.cancel_reason == "quiet_hours_cutoff"
        else:
            assert saved.status == "encoding"
            assert saved.cancel_requested is False


def test_web_stop_request_is_persisted_for_standalone_worker(tmp_path, monkeypatch, probe_facts):
    application, _, _, spawned = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        processor.stop_job(job.id)
        persisted = processor.queue._find(job.id)
        assert persisted.status == "stopping"
        assert persisted.cancel_requested is True
        assert persisted.cancel_reason == "user_stop"
        assert spawned == []  # The web-side worker never owns or kills ffmpeg.
        processor.queue.cancelled_real_job(job.id)
        finished = processor.queue._find(job.id)
        assert finished.status == "skipped"
        assert finished.cancel_requested is False


def test_stale_source_is_rejected_before_popen(tmp_path, monkeypatch, probe_facts):
    application, source_path, _, spawned = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        with source_path.open("ab") as handle:
            handle.write(b"changed")
        worker = processor.queue.worker
        with pytest.raises(Exception, match="changed since its revision was captured"):
            worker._execute(job)
        assert spawned == []
        processor.queue.fail_real_job(added["id"], "stale source refused")
        saved = next(item for item in processor.get_queue()["history"] if item["id"] == added["id"])
        assert saved["status"] == "failed" and saved["measured_saving"] is None


@pytest.mark.parametrize("tag", ["Preserve A/V", "Preserve Video"])
def test_existing_preserve_video_policies_still_block_filesystem_queue(tmp_path, monkeypatch, probe_facts, tag):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        from app.models.tags import TagTarget, TagUpdate
        processor.update_tags(TagUpdate(targets=[TagTarget(kind="movie", id=movie.id)], tags=[tag]))
        result = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))
        assert not result["added"]
        assert any(tag in reason for reason in result["excluded"][0]["reasons"])


def test_web_restart_does_not_recover_or_stop_active_real_job(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    config = Settings(media_backend="filesystem", movies_root=tmp_path / "movies",
        database_path=tmp_path / "state.sqlite3", workspace_root=tmp_path / "workspace",
        ffmpeg_binary="/fake/ffmpeg", ffprobe_binary="/fake/ffprobe")

    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        claimed = processor.queue.claim_next("cpu")
        assert claimed is not None and claimed.status == "encoding"

    # A new WebUI process sees the worker-owned active state but must not reset it.
    with TestClient(create_app(config=config, start_workers=False)) as client:
        state = client.get("/api/queue").json()
        active = next(lane["active"] for lane in state["lanes"] if lane["backend"] == "cpu")
        assert active["id"] == added["id"]
        assert active["status"] == "encoding"


def build_gpu_show_processor(tmp_path, monkeypatch, probe_facts):
    """Worker-process GPU lane over one show episode probed as probe_facts.

    Returns the processor and the list of spawned fake ffmpeg processes."""
    shows_root = tmp_path / "shows"
    series = shows_root / "Fixture Show"
    series.mkdir(parents=True)
    source = series / "Fixture Show S01E01.mkv"
    with source.open("wb") as handle:
        handle.truncate(400_000_000)
    workspace = tmp_path / "workspace"
    (workspace / "jobs").mkdir(parents=True)
    config = Settings(
        media_backend="filesystem",
        shows_root=shows_root,
        database_path=tmp_path / "state.sqlite3",
        workspace_root=workspace,
        gpu_device=Path("/dev/dri/renderD128"),
        ffmpeg_binary="/fake/ffmpeg",
        ffprobe_binary="/fake/ffprobe",
    )

    from app import container
    monkeypatch.setattr(container, "capability_status", lambda *args: {
        "available": True,
        "cpu_available": True,
        "gpu_available": True,
        "supported_backends": ["cpu", "gpu"],
        "ffmpeg_available": True,
        "ffprobe_available": True,
        "libx265_available": True,
        "hevc_vaapi_available": True,
        "gpu_device": "/dev/dri/renderD128",
        "ffmpeg_binary": "/fake/ffmpeg",
        "ffprobe_binary": "/fake/ffprobe",
        "workspace_root": str(workspace),
        "workspace_writable": True,
        "cpu_unavailable_reason": None,
        "gpu_unavailable_reason": None,
        "unavailable_reason": None,
    })
    output_probe = probe_facts.model_copy(update={"streams": [
        stream.model_copy(update={
            "codec": "hevc",
            "pixel_format": "yuv420p10le", "profile": "Main 10",
            "hdr": stream.hdr.model_copy(update={"bit_depth": 10}),
        }) if stream.kind == "video" else stream
        for stream in probe_facts.streams
    ]})

    def inspect(self, path):
        return output_probe if ".kompressor.partial.mkv" in path.name else probe_facts

    monkeypatch.setattr(FFprobeService, "inspect", inspect)
    monkeypatch.setattr(FFmpegEncoder, "packet_hashes",
                        lambda self, job_id, path, selectors: [f"track-{i}" for i in range(len(selectors))])
    monkeypatch.setattr(FFmpegEncoder, "decode_errors", lambda self, job_id, path, **frames: [])
    spawned = []

    def popen(command, **kwargs):
        process = FakeProcess(command)
        spawned.append(process)
        return process

    monkeypatch.setattr("app.workers.ffmpeg.subprocess.Popen", popen)

    processor = build_media_processor(config, process_role="worker", worker_backend="gpu")
    return processor, spawned


def test_gpu_worker_claims_validates_and_completes_show_job(tmp_path, monkeypatch, probe_facts):
    processor, spawned = build_gpu_show_processor(tmp_path, monkeypatch, probe_facts)
    assert processor.queue.supported_backends == frozenset({"gpu"})
    assert isinstance(processor.queue.worker, RealEncoderWorker)
    assert processor.queue.worker.supported_backends == frozenset({"gpu"})
    processor.scan_library()
    library = processor.get_library()
    episode = library.shows[0].seasons[0].episodes[0]
    added = processor.queue_encode(EnqueueRequest(
        media_ids=[episode.id], scope="show", preset_id="show-streaming-quality"))["added"][0]

    job = processor.queue.claim_next("gpu")
    assert job is not None and job.backend == "gpu"
    processor.queue.worker._execute(job)

    saved = next(item for item in processor.get_queue()["history"] if item["id"] == added["id"])
    assert saved["status"] == "completed"
    assert saved["backend"] == "gpu"
    assert saved["output_size"] == 100_000_000
    assert spawned
    command = spawned[0].command
    assert command[command.index("-c:v:0") + 1] == "hevc_vaapi"
    assert command[command.index("-global_quality:v:0") + 1] == "23"
    assert command[command.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD128"


def test_background_worker_claims_and_completes_without_http_encode(tmp_path, monkeypatch, probe_facts):
    output_probe = probe_facts.model_copy(update={"streams": [
        stream.model_copy(update={"codec": "hevc", "pixel_format": "yuv420p10le", "hdr": stream.hdr.model_copy(update={"bit_depth": 10})}) if stream.kind == "video" else stream
        for stream in probe_facts.streams]})
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts,
        output_probe, start_workers=True)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        import time
        saved = None
        for _ in range(200):
            saved = next((job for job in processor.get_queue()["history"] if job["id"] == added["id"]), None)
            if saved is not None:
                break
            time.sleep(0.01)
        assert saved is not None and saved["status"] == "completed"


def test_worker_recovery_requeues_active_job_during_full_runtime_outage(
        tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(
            media_ids=[movie.id], scope="movie", preset_id="movie-streaming-quality"))["added"][0]
        active = processor.queue.claim_next("cpu")
        assert active is not None and active.status == "encoding"

    from app import container
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(container, "capability_status", lambda *args: {
        "available": False,
        "cpu_available": False,
        "gpu_available": False,
        "supported_backends": [],
        "ffmpeg_available": False,
        "ffprobe_available": False,
        "libx265_available": False,
        "hevc_vaapi_available": False,
        "gpu_device": "/dev/dri/renderD128",
        "ffmpeg_binary": "/fake/ffmpeg",
        "ffprobe_binary": "/fake/ffprobe",
        "workspace_root": str(workspace),
        "workspace_writable": False,
        "cpu_unavailable_reason": "runtime unavailable",
        "gpu_unavailable_reason": "runtime unavailable",
        "unavailable_reason": "runtime unavailable",
    })
    config = Settings(
        media_backend="filesystem",
        movies_root=tmp_path / "movies",
        database_path=tmp_path / "state.sqlite3",
        workspace_root=workspace,
        ffmpeg_binary="/fake/ffmpeg",
        ffprobe_binary="/fake/ffprobe",
    )
    restarted = create_app(
        config=config, start_workers=False, start_real_worker=True)
    with TestClient(restarted):
        processor = restarted.state.media_processor
        saved = processor.queue._find(added["id"])
        assert saved.status == "queued"
        assert saved.finished_at is None
        assert not saved.reasons


def test_recovery_requeues_interrupted_job_and_removes_stale_outputs(tmp_path, monkeypatch, probe_facts):
    application, _, workspace, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job_id = added["id"]
        active = processor.queue.claim_next("cpu")
        assert active is not None
        job_dir = workspace / "jobs" / job_id
        job_dir.mkdir(mode=0o700)
        (job_dir / "Fixture.kompressor.partial.mkv").write_bytes(b"partial")
        (job_dir / "Fixture.kompressor.mkv").write_bytes(b"unvalidated")
        processor.queue.recover()
        saved = next(job for job in processor.queue.repository.get_all() if job.id == job_id)
        assert saved.status == "queued" and saved.progress == 0
        assert saved.output_path is None and saved.measured_saving is None
        assert not list(job_dir.glob("*.kompressor*.mkv"))


def test_stop_terminates_only_owned_process_and_removes_partial(tmp_path):
    class StubbornProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.wait_calls = []
        def poll(self): return self.returncode
        def terminate(self): self.terminated = True
        def kill(self): self.killed = True; self.returncode = -9
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if not self.killed:
                raise subprocess.TimeoutExpired("ffmpeg", timeout if timeout is not None else 0)
            return self.returncode

    workspace = tmp_path / "workspace"
    job_dir = workspace / "jobs" / "job-stop"
    job_dir.mkdir(parents=True, mode=0o700)
    partial = job_dir / "Film.kompressor.partial.mkv"
    final = job_dir / "Film.kompressor.mkv"
    partial.write_bytes(b"partial")
    final.write_bytes(b"already-promoted")
    encoder = FFmpegEncoder("ffmpeg", workspace, stop_grace_seconds=0.01)
    owned, unrelated = StubbornProcess(), StubbornProcess()
    encoder._processes["job-stop"] = cast(subprocess.Popen[str], owned)
    encoder._processes["other-job"] = cast(subprocess.Popen[str], unrelated)
    encoder._paths["job-stop"] = (partial, final)
    encoder.stop("job-stop")
    assert owned.terminated and owned.killed
    assert owned.wait_calls == [0.01, 2]
    assert not unrelated.terminated and not unrelated.killed
    assert not partial.exists()
    assert final.read_bytes() == b"already-promoted"


def test_stop_request_survives_until_the_jobs_own_cleanup(tmp_path):
    encoder = FFmpegEncoder("ffmpeg", tmp_path / "workspace")
    encoder.stop("job-stop")
    # The encode thread checks this after ffmpeg exits; stop() must not clear it.
    assert "job-stop" in encoder._cancelled
    encoder.cleanup("job-stop")
    assert "job-stop" not in encoder._cancelled


def claimed_movie_job(application, preset_id="movie-streaming-quality"):
    processor = application.state.media_processor
    processor.scan_library()
    movie = processor.get_library().movies[0]
    result = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie", preset_id=preset_id))
    job = processor.queue.claim_next("cpu")
    assert job is not None and job.id == result["added"][0]["id"]
    return processor, job


def history_entry(processor, job_id):
    return next(item for item in processor.get_queue()["history"] if item["id"] == job_id)


def test_stop_during_encode_is_skipped_although_ffmpeg_exits_with_an_error(tmp_path, monkeypatch, probe_facts):
    # SIGTERM makes ffmpeg exit 255; that exit status is the stop, not a failure.
    application, source, workspace, _ = build_real_app(tmp_path, monkeypatch, probe_facts, exit_code=255)
    with TestClient(application):
        processor, job = claimed_movie_job(application)
        worker = processor.queue.worker
        original_progress = processor.queue.update_real_progress

        def progress(job_id, percent, elapsed):
            original_progress(job_id, percent, elapsed)
            if not worker.cancelled_by_request(job_id):
                processor.queue.skip(job_id)  # Web process: persisted request.
                worker.stop(job_id)           # Worker control loop observes it.
        processor.queue.update_real_progress = progress
        worker._run_job(job)
        saved = history_entry(processor, job.id)
    assert saved["status"] == "skipped", saved
    assert "Stopped by user." in saved["reasons"]
    assert saved["error_message"] is None and saved["ffmpeg_exit_code"] is None
    assert not list((workspace / "jobs" / job.id).glob("*.mkv"))
    assert source.stat().st_size == 400_000_000


def test_stop_during_output_validation_is_skipped_not_failed(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts, probe_facts)
    with TestClient(application):
        processor, job = claimed_movie_job(application)
        worker = processor.queue.worker

        def inspect(self, path):
            # Stop & Skip removes the partial while ffprobe is reading it.
            processor.queue.skip(job.id)
            worker.stop(job.id)
            raise Conflict("Output file is missing.")
        monkeypatch.setattr(FFprobeService, "inspect", inspect)
        worker._run_job(job)
        saved = history_entry(processor, job.id)
    assert saved["status"] == "skipped", saved
    assert saved["error_message"] is None and saved["validation_errors"] == []


def test_worker_shutdown_mid_encode_leaves_the_job_for_restart_recovery(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts, exit_code=255)
    with TestClient(application):
        processor, job = claimed_movie_job(application)
        worker = processor.queue.worker
        original_progress = processor.queue.update_real_progress

        def progress(job_id, percent, elapsed):
            original_progress(job_id, percent, elapsed)
            worker._stop.set()  # As shutdown() does before stopping owned ffmpeg.
            worker.encoder.stop(job_id)
        processor.queue.update_real_progress = progress
        worker._run_job(job)
        current = processor.queue._find(job.id)
    # Not failed: the next worker start requeues it from zero.
    assert current.status == "encoding" and current.error_message is None


def test_filesystem_enqueue_returns_runtime_error_when_encoder_prerequisites_fail(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts,
        runtime_available=False)
    with TestClient(application) as client:
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        response = client.post("/api/queue", json={"media_ids": [movie.id], "scope": "movie",
            "preset_id": "movie-streaming-quality"})
        assert response.status_code == 422
        assert "Real encoding is unavailable" in response.json()["detail"]
        diagnostics = client.get("/api/settings/runtime").json()
        assert diagnostics["encoding_enabled"] is False
        assert diagnostics["workspace_root"] == str(tmp_path / "workspace")
        lanes = {lane["backend"]: lane for lane in client.get("/api/queue").json()["lanes"]}
        assert lanes["cpu"]["available"] is False
        assert lanes["gpu"]["available"] is False


def test_modal_eligibility_includes_real_execution_capability_blockers(
        tmp_path, monkeypatch, probe_facts):
    hdr_streams = [
        stream.model_copy(update={
            "pixel_format": "yuv420p10le",
            "hdr": HDRSignalling(
                transfer="smpte2084", primaries="bt2020", matrix="bt2020nc",
                bit_depth=10, inspection_complete=True,
                dolby_vision_rpu=False, hdr10plus_metadata=False,
            ),
        }) if stream.kind == "video" else stream
        for stream in probe_facts.streams
    ]
    hdr_probe = probe_facts.model_copy(update={"streams": hdr_streams})
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, hdr_probe)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        result = processor.evaluate_compression(
            movie.id, "movie", "movie-streaming-quality")
        assert result.eligible is False
        assert any("confirmed SDR" in reason for reason in result.reasons)


def test_modal_eligibility_reports_unavailable_real_lane(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, runtime_available=False)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        result = processor.evaluate_compression(
            movie.id, "movie", "movie-streaming-quality")
        assert result.eligible is False
        assert any("CPU real encoding is not available" in reason for reason in result.reasons)


def test_queue_snapshot_marks_gpu_unavailable_when_only_cpu_runtime_exists(
        tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application) as client:
        lanes = {lane["backend"]: lane for lane in client.get("/api/queue").json()["lanes"]}
        assert lanes["cpu"]["available"] is True
        assert lanes["gpu"]["available"] is False


def test_worker_controls_mark_unavailable_lane_as_not_accepting_jobs(
        tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application) as client:
        controls = client.get("/api/queue/workers").json()["lanes"]
        assert controls["cpu"]["available"] is True
        assert controls["cpu"]["accepting_jobs"] is True
        assert controls["gpu"]["available"] is False
        assert controls["gpu"]["accepting_jobs"] is False


def test_preserve_audio_tag_overrides_modal_and_keeps_all_tracks_copied(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        from app.models.tags import TagTarget, TagUpdate
        processor.update_tags(TagUpdate(targets=[TagTarget(kind="movie", id=movie.id)], tags=["Preserve Audio"]))
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality", preserve_audio=False))["added"]
        assert len(added) == 1
        assert added[0]["preserve_audio"] is True


def test_library_root_cannot_overlap_output_workspace(tmp_path, monkeypatch, probe_facts):
    application, _, workspace, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        from app.models.preferences import LibraryPaths
        from app.services.errors import InvalidOperation
        with pytest.raises(InvalidOperation, match="must not overlap"):
            processor.update_library_paths(LibraryPaths(movies_path=str(workspace), shows_path=""))


def test_invalid_output_is_failed_with_validation_reasons_and_cleaned(tmp_path, monkeypatch, probe_facts):
    application, _, workspace, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        job = processor.queue.claim_next("cpu")
        assert job is not None
        worker = processor.queue.worker
        with pytest.raises(Conflict, match="Output validation failed"):
            worker._execute(job)
        processor.queue.fail_real_job(added["id"], "Output validation failed")
        worker.encoder.cleanup(added["id"], remove_final=True)
        saved = next(item for item in processor.get_queue()["history"] if item["id"] == added["id"])
        assert saved["status"] == "failed"
        assert saved["validation_errors"]
        assert saved["measured_saving"] is None
        assert not list((workspace / "jobs" / added["id"]).glob("*.kompressor*.mkv"))


def test_real_worker_blocks_persisted_fake_jobs_instead_of_encoding(tmp_path, monkeypatch, probe_facts):
    application, _, _, spawned = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality"))["added"][0]
        saved = processor.queue._find(added["id"])
        saved.execution_mode = "fake"
        processor.queue.repository.save(saved)
        processor.queue.recover()
        blocked = processor.queue._find(added["id"])
        assert blocked.status == "blocked"
        assert "cannot run in real mode" in blocked.reasons[0]
        assert spawned == []


def test_data_streams_are_not_mapped_or_expected_in_the_matroska_output(probe_facts, tmp_path):
    # MP4/MOV timecode tracks probe as data; the Matroska muxer rejects them.
    timecode = StreamFacts(index=len(probe_facts.streams), kind="data", codec="bin_data")
    source = probe_facts.model_copy(update={"streams": [*probe_facts.streams, timecode]})
    job = queue_job(preserve_subtitles=True)
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/read-only/Film.mkv"),
                                          tmp_path / "Fixture.kompressor.partial.mkv", source)
    assert f"0:{timecode.index}" not in command
    output = probe_facts.model_copy(update={"streams": [
        stream.model_copy(update={"codec": "hevc", "pixel_format": "yuv420p10le",
                                  "hdr": stream.hdr.model_copy(update={"bit_depth": 10})})
        if stream.kind == "video" else stream for stream in probe_facts.streams]})
    output_file = tmp_path / "output.mkv"
    output_file.write_bytes(b"validated")
    errors = CPUEncodeCapability().validate_output(movie_item(source), job, output, output_file)
    assert not any("data" in error or "order" in error for error in errors), errors
