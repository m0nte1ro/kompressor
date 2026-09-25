import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.models.inventory import SourceReference
from app.models.media import AudioTrack, Movie
from app.models.preset import CompressionPreset
from app.models.probe import HDRSignalling, MediaProbeResult
from app.models.queue import EnqueueRequest, QueueJob
from app.repositories.preset_seed import SeedPresetRepository
from app.services.encoding_capability import CPUEncodeCapability
from app.services.errors import Conflict
from app.services.ffprobe import FFprobeService
from app.workers.ffmpeg import FFmpegEncoder, FFmpegError


@pytest.fixture
def probe_facts():
    payload = json.loads((PROJECT_ROOT / "fixtures/ffprobe/progressive.json").read_text())
    from app.services.ffprobe import parse_ffprobe
    return parse_ffprobe(payload)


def movie_preset() -> CompressionPreset:
    presets = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    return next(preset for preset in presets if preset.id == "movie-streaming-quality")


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
    return QueueJob(**values)


def movie_item(probe, **changes) -> Movie:
    values = dict(id="file-test-1", media_id="logical-test-1", revision_id="revision-1",
        processing_supported=True, probe=probe, name="Fixture movie", year=1990,
        path="/readonly/Fixture.mkv", source=None, width=1920, height=1080,
        resolution="1080p", video_codec="h264", video_bitrate=12_000_000,
        duration_seconds=120.5, hdr=None, interlaced=False, size=400_000_000,
        audio=[], hardlinks=1)
    values.update(changes)
    return Movie(**values)


def test_runtime_capability_checks_ffmpeg_ffprobe_workspace_and_x265(tmp_path, monkeypatch):
    from app.services import encoding_runtime
    workspace = tmp_path / "workspace"
    (workspace / "jobs").mkdir(parents=True)
    movie_root = tmp_path / "movies"
    movie_root.mkdir()
    monkeypatch.setattr(encoding_runtime, "executable", lambda binary: f"/fake/{binary}")
    monkeypatch.setattr(encoding_runtime.FFprobeService, "runtime_check", lambda binary: (True, None))
    monkeypatch.setattr(encoding_runtime.FFmpegEncoder, "runtime_check", lambda binary: (True, None))
    status = encoding_runtime.capability_status("ffmpeg", "ffprobe", workspace, {"movie": movie_root})
    assert status["available"] and status["ffprobe_available"] and status["libx265_available"]
    assert status["workspace_writable"]


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
    mapped = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-map"]
    assert mapped == [f"0:{stream.index}" for stream in probe_facts.streams]
    assert "-c" in command and command[command.index("-c") + 1] == "copy"
    assert command[command.index("-map_metadata") + 1] == "0"
    assert command[command.index("-map_chapters") + 1] == "0"
    assert command[-1] == str(partial)


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
        (item, job.model_copy(update={"backend": "qsv"}), "CPU backend only"),
        (item, job.model_copy(update={"replace_source": True}), "Source replacement is disabled"),
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


def test_output_validation_reports_codec_resolution_duration_and_stream_loss(probe_facts, tmp_path):
    guard = CPUEncodeCapability()
    job = queue_job()
    item = movie_item(probe_facts)
    output_file = tmp_path / "partial.mkv"
    output_file.write_bytes(b"fake")
    changed_streams = [
        stream.model_copy(update={"codec": "h264", "width": 1280}) if stream.kind == "video" else stream
        for stream in probe_facts.streams if stream.kind != "attachment"
    ]
    invalid = probe_facts.model_copy(update={"streams": changed_streams, "duration_seconds": 10})
    errors = guard.validate_output(item, job, invalid, output_file)
    assert any("expected HEVC" in error for error in errors)
    assert any("resolution" in error for error in errors)
    assert any("duration" in error for error in errors)
    assert any("attachment streams" in error for error in errors)


class FakeProcess:
    def __init__(self, command, *, exit_code=0, output_size=100_000_000, stderr=""):
        self.command = command
        self.stdout = io.StringIO("out_time_us=60000000\nprogress=continue\nout_time_us=120500000\nprogress=end\n")
        self.stderr = io.StringIO(stderr)
        self.returncode = exit_code
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
                   start_workers=False, runtime_available=True):
    root = tmp_path / "movies"
    root.mkdir()
    source = root / "Fixture.mkv"
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
        return output_probe if ".kompressor.partial.mkv" in path.name and output_probe else source_probe
    monkeypatch.setattr(FFprobeService, "inspect", inspect)
    spawned = []
    def popen(command, **kwargs):
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        process = FakeProcess(command, exit_code=exit_code, stderr="fixture ffmpeg error" if exit_code else "")
        spawned.append(process)
        return process
    monkeypatch.setattr("app.workers.ffmpeg.subprocess.Popen", popen)
    application = create_app(config=config, start_workers=start_workers)
    return application, source, workspace, spawned


def test_real_worker_validates_promotes_and_measures_output(tmp_path, monkeypatch, probe_facts):
    output_streams = [stream.model_copy(update={"codec": "hevc"}) if stream.kind == "video" else stream
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
            original_complete(job_id, path, size)
            states.append(processor.queue._find(job_id).status)
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


def test_background_worker_claims_and_completes_without_http_encode(tmp_path, monkeypatch, probe_facts):
    output_probe = probe_facts.model_copy(update={"streams": [
        stream.model_copy(update={"codec": "hevc"}) if stream.kind == "video" else stream
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
        for _ in range(200):
            saved = next((job for job in processor.get_queue()["history"] if job["id"] == added["id"]), None)
            if saved is not None:
                break
            time.sleep(0.01)
        assert saved is not None and saved["status"] == "completed"


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
                import subprocess
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
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
    encoder._processes["job-stop"] = owned
    encoder._processes["other-job"] = unrelated
    encoder._paths["job-stop"] = (partial, final)
    encoder.stop("job-stop")
    assert owned.terminated and owned.killed
    assert owned.wait_calls == [0.01, 2]
    assert not unrelated.terminated and not unrelated.killed
    assert not partial.exists()
    assert final.read_bytes() == b"already-promoted"


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
