"""Source replacement, bit-exact audio verification and safe audio/lane defaults."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.models.media import AudioTrack
from app.models.probe import HDRSignalling, MediaProbeResult, StreamFacts
from app.models.queue import EnqueueRequest
from app.models.tags import TagTarget, TagUpdate
from app.services.encoding_capability import CPUEncodeCapability
from app.services.errors import Conflict
from app.services.estimation import plan_audio_tracks
from app.services.policy import PolicyEngine
from app.workers import replacement
from app.workers.ffmpeg import FFmpegEncoder, FFmpegError
from app.workers.replacement import ReplacementError, SourceReplacer, replacement_reasons
from tests.test_real_encoding import (build_real_app, movie_item, probe_facts, qsv_preset,  # noqa: F401
                                      queue_job)
from fastapi.testclient import TestClient


def hevc_output(probe):
    return probe.model_copy(update={"streams": [
        stream.model_copy(update={"codec": "hevc", "pixel_format": "yuv420p10le",
                                  "hdr": stream.hdr.model_copy(update={"bit_depth": 10})})
        if stream.kind == "video" else stream for stream in probe.streams]})


def audio_tracks(probe):
    return [AudioTrack(codec=s.codec, channels=s.channels, bitrate=s.bitrate, language=s.language,
                       title=s.title, stream_index=s.index, channel_layout=s.channel_layout,
                       dispositions=s.dispositions) for s in probe.streams if s.kind == "audio"]


def run_one(processor, **request):
    """Queue, claim and execute one movie job the way the worker loop does."""
    processor.scan_library()
    movie = processor.get_library().movies[0]
    result = processor.queue_encode(EnqueueRequest(
        media_ids=[movie.id], scope="movie", preset_id="movie-streaming-quality", **request))
    assert result["added"], result["excluded"]
    job = processor.queue.claim_next("cpu")
    worker = processor.queue.worker
    try:
        worker._execute(job)
    except Exception as error:
        processor.queue.fail_real_job(job.id, str(error))
        worker.encoder.cleanup(job.id, remove_final=True)
    finally:
        worker.encoder.cleanup(job.id)
    return next(item for item in processor.get_queue()["history"] if item["id"] == job.id)


def workspace_outputs(workspace):
    return [path for path in (workspace / "jobs").rglob("*.mkv")]


# --- Replacement end to end through the worker -------------------------------------------


def test_replace_swaps_in_verified_output_with_source_owner_and_mode(tmp_path, monkeypatch, probe_facts):
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)
    os.chmod(source, 0o640)
    before = source.stat()
    with TestClient(application):
        saved = run_one(application.state.media_processor, replace_source=True)
    assert saved["status"] == "completed", saved
    assert saved["source_replaced"] is True
    assert saved["output_path"] == str(source)
    assert saved["measured_saving"] == 400_000_000 - 1_000_000
    after = source.stat()
    assert after.st_size == 1_000_000 and after.st_ino != before.st_ino
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (before.st_uid, before.st_gid, 0o640)
    assert sorted(source.parent.iterdir()) == [source]  # No backup or staging file left behind.
    assert not workspace_outputs(workspace)


def test_replace_rolls_back_when_the_file_at_the_source_path_fails_validation(
        tmp_path, monkeypatch, probe_facts):
    # The source path is re-probed after the swap; here it reports the old H.264 video.
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000,
        replaced_probe=probe_facts)
    before = source.stat()
    with TestClient(application):
        saved = run_one(application.state.media_processor, replace_source=True)
    assert saved["status"] == "failed" and saved["source_replaced"] is False
    assert "final validation" in saved["error_message"]
    assert "original restored" in saved["error_message"]
    after = source.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert sorted(source.parent.iterdir()) == [source]
    assert not workspace_outputs(workspace)


def test_replace_keeps_source_when_measured_saving_is_below_the_preset_minimum(
        tmp_path, monkeypatch, probe_facts):
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=390_000_000)
    before = source.stat()
    with TestClient(application):
        saved = run_one(application.state.media_processor, replace_source=True)
    assert saved["status"] == "skipped" and saved["source_replaced"] is False
    assert any("below the preset minimum of 20%" in reason for reason in saved["reasons"])
    assert saved["measured_saving"] == 10_000_000
    assert source.stat().st_ino == before.st_ino and source.stat().st_size == before.st_size
    assert not workspace_outputs(workspace)


def test_copied_audio_hash_mismatch_fails_the_job_and_never_touches_the_source(
        tmp_path, monkeypatch, probe_facts):
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)

    def packet_hashes(self, job_id, path, selectors):
        salt = "output" if ".kompressor" in Path(path).name else "source"
        return [f"{salt}-{ordinal}" for ordinal in range(len(selectors))]

    monkeypatch.setattr(FFmpegEncoder, "packet_hashes", packet_hashes)
    before = source.stat()
    with TestClient(application):
        saved = run_one(application.state.media_processor, replace_source=True)
    assert saved["status"] == "failed"
    assert any("not bit-identical" in error for error in saved["validation_errors"])
    assert source.stat().st_ino == before.st_ino and source.stat().st_size == before.st_size
    assert sorted(source.parent.iterdir()) == [source]
    assert not workspace_outputs(workspace)


def test_keep_original_jobs_still_verify_copied_audio(tmp_path, monkeypatch, probe_facts):
    application, source, _, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)
    calls = []

    def packet_hashes(self, job_id, path, selectors):
        calls.append((Path(path).name, selectors))
        return [f"same-{ordinal}" for ordinal in range(len(selectors))]

    monkeypatch.setattr(FFmpegEncoder, "packet_hashes", packet_hashes)
    with TestClient(application):
        saved = run_one(application.state.media_processor, replace_source=False)
    assert saved["status"] == "completed" and saved["source_replaced"] is False
    # Both audio tracks are copied by default: source by index, output by audio ordinal.
    assert calls == [("Fixture.mkv", ["0:1", "0:2"]), ("Fixture.kompressor.partial.mkv", ["0:a:0", "0:a:1"])]
    assert source.stat().st_size == 400_000_000


def test_replacement_is_refused_up_front_for_non_mkv_sources(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts),
                                          source_name="Fixture.mp4")
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        preview = processor.evaluate_compression(movie.id, "movie", "movie-streaming-quality",
                                                 preserve_audio=True, replace_source=True)
        assert not preview.eligible and any("requires an MKV source" in r for r in preview.reasons)
        result = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
                                                       preset_id="movie-streaming-quality", replace_source=True))
        assert not result["added"] and "requires an MKV source" in " ".join(result["excluded"][0]["reasons"])
        kept = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
                                                     preset_id="movie-streaming-quality"))
        assert kept["added"]


def test_replacing_job_cannot_be_stopped_or_blocked_by_policy_changes(tmp_path, monkeypatch, probe_facts):
    application, source, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts))
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
                                              preset_id="movie-streaming-quality", replace_source=True))
        job = processor.queue.claim_next("cpu")
        assert processor.queue.set_real_validating(job.id, "/tmp/unused.mkv")
        journal = SourceReplacer().plan(job.id, source, 1_000_000)
        assert processor.queue.set_real_replacing(job.id, journal)
        with pytest.raises(Conflict, match="cannot be interrupted"):
            processor.stop_job(job.id)
        assert processor.stop_active_worker("cpu") is False
        processor.update_tags(TagUpdate(targets=[TagTarget(kind="movie", id=movie.id)],
                                        tags=["Preserve A/V"], operation="add"))
        current = processor.queue._find(job.id)
        assert current.status == "replacing" and not current.cancel_requested


def test_worker_restart_completes_a_verified_swap_and_restores_an_unverified_one(
        tmp_path, monkeypatch, probe_facts):
    application, source, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts))
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
                                              preset_id="movie-streaming-quality", replace_source=True))
        job = processor.queue.claim_next("cpu")
        assert processor.queue.set_real_validating(job.id, "/tmp/unused.mkv")
        # Simulate a kill right after the swap was verified but before the backup was removed.
        journal = SourceReplacer().plan(job.id, source, 3)
        incoming = Path(journal.incoming)
        incoming.write_bytes(b"new")
        info = incoming.stat()
        journal = journal.model_copy(update={"incoming_device": info.st_dev, "incoming_inode": info.st_ino,
                                             "phase": "verified"})
        assert processor.queue.set_real_replacing(job.id, journal)
        os.rename(source, journal.backup)
        os.rename(incoming, source)

        processor.queue.recover(backends={"cpu"})
        saved = processor.queue._find(job.id)
        assert saved.status == "completed" and saved.source_replaced and saved.output_size == 3
        assert source.read_bytes() == b"new" and sorted(source.parent.iterdir()) == [source]


# --- SourceReplacer: failure paths and crash recovery on real files ----------------------


def staged(tmp_path, *, phase="copied"):
    directory = tmp_path / "library"
    directory.mkdir()
    target = directory / "Film.mkv"
    target.write_bytes(b"original")
    output = tmp_path / "output.mkv"
    output.write_bytes(b"replacement")
    journal = SourceReplacer().plan("job-1", target, output.stat().st_size)
    return target, output, journal


def replace(target, output, journal, *, verify=lambda path: [], before_swap=lambda: None):
    saved = []
    notes = SourceReplacer().replace(journal, output, before_swap=before_swap, verify=verify,
                                     save=lambda state: saved.append(state.phase))
    return notes, saved


def test_replacer_swaps_and_journals_every_step(tmp_path):
    target, output, journal = staged(tmp_path)
    notes, phases = replace(target, output, journal)
    assert notes == [] and target.read_bytes() == b"replacement"
    assert phases == ["copying", "copying", "copied", "swapping", "swapped", "verified"]
    assert sorted(target.parent.iterdir()) == [target]


def test_replacer_restores_original_when_the_swap_rename_fails(tmp_path, monkeypatch):
    target, output, journal = staged(tmp_path)
    real_rename = os.rename

    def rename(source, destination):
        if str(source) == journal.incoming:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(source, destination)

    monkeypatch.setattr(replacement.os, "rename", rename)
    with pytest.raises(ReplacementError, match="The original was restored"):
        replace(target, output, journal)
    monkeypatch.setattr(replacement.os, "rename", real_rename)
    assert target.read_bytes() == b"original"
    assert sorted(target.parent.iterdir()) == [target]


def test_replacer_leaves_original_untouched_when_the_pre_swap_guard_fails(tmp_path):
    target, output, journal = staged(tmp_path)

    def guard():
        raise Conflict("Source is hardlinked or its hardlink count is unknown.")

    with pytest.raises(ReplacementError, match="hardlinked.*The original was not modified"):
        replace(target, output, journal, before_swap=guard)
    assert target.read_bytes() == b"original" and sorted(target.parent.iterdir()) == [target]


def test_replacer_refuses_to_start_when_a_staging_file_exists(tmp_path):
    target, output, journal = staged(tmp_path)
    Path(journal.backup).write_bytes(b"someone else's file")
    with pytest.raises(ReplacementError, match="staging file already exists"):
        replace(target, output, journal)
    assert target.read_bytes() == b"original"
    assert Path(journal.backup).read_bytes() == b"someone else's file"


def crash_state(tmp_path, state):
    target, output, journal = staged(tmp_path)
    incoming, backup = Path(journal.incoming), Path(journal.backup)
    if state == "copying":
        incoming.write_bytes(b"repl")  # partial copy, inode not yet journalled
        return target, journal.model_copy(update={"phase": "copying"})
    incoming.write_bytes(b"replacement")
    info = incoming.stat()
    journal = journal.model_copy(update={"incoming_device": info.st_dev, "incoming_inode": info.st_ino})
    if state == "copied":
        return target, journal.model_copy(update={"phase": "copied"})
    os.rename(target, backup)
    if state == "between-renames":
        return target, journal.model_copy(update={"phase": "swapping"})
    os.rename(incoming, target)
    if state == "swapped":
        return target, journal.model_copy(update={"phase": "swapped"})
    if state == "verified":
        return target, journal.model_copy(update={"phase": "verified"})
    backup.unlink()
    return target, journal.model_copy(update={"phase": "verified"})


@pytest.mark.parametrize("state, outcome, content", [
    ("copying", "untouched", b"original"),
    ("copied", "untouched", b"original"),
    ("between-renames", "restored", b"original"),
    ("swapped", "restored", b"original"),
    ("verified", "replaced", b"replacement"),
    ("backup-removed", "replaced", b"replacement"),
])
def test_crash_recovery_resolves_every_interruption_point(tmp_path, state, outcome, content):
    target, journal = crash_state(tmp_path, state)
    assert SourceReplacer().resolve(journal, finalize=True)[0] == outcome
    assert target.read_bytes() == content
    assert sorted(target.parent.iterdir()) == [target]


def test_crash_recovery_never_deletes_files_of_unknown_identity(tmp_path):
    target, journal = crash_state(tmp_path, "between-renames")
    target.write_bytes(b"something else appeared here")
    outcome, message = SourceReplacer().resolve(journal, finalize=True)
    assert outcome == "manual" and "MANUAL CHECK REQUIRED" in message
    assert target.read_bytes() == b"something else appeared here"
    assert Path(journal.backup).read_bytes() == b"original"


def test_replacement_reasons_require_mkv_and_a_writable_directory(tmp_path, monkeypatch):
    mkv, mp4 = tmp_path / "a.mkv", tmp_path / "a.mp4"
    assert replacement_reasons(mkv) == []
    assert any("MKV" in reason for reason in replacement_reasons(mp4))
    monkeypatch.setattr(replacement.os, "access", lambda path, mode: False)
    assert any("not writable" in reason for reason in replacement_reasons(mkv))


# --- Audio: never converted by default, planned once, verified bit-exact ------------------


def test_audio_is_preserved_unless_the_job_explicitly_opts_out(probe_facts):
    preset = qsv_preset(efficient_audio=True)
    assert preset.preserve_audio_by_default is False  # The preset default no longer matters.
    item = movie_item(probe_facts, audio=audio_tracks(probe_facts))

    def evaluate(preserve_audio, tags=()):
        return PolicyEngine().evaluate(item=item, scope="show", preset=preset, effective_tags=list(tags),
                                       preserve_audio=preserve_audio, preserve_subtitles=True)

    default = evaluate(None)
    assert default.preserve_audio is True and {t["action"] for t in default.audio_plan} == {"copy"}
    opted_out = evaluate(False)
    assert opted_out.preserve_audio is False and opted_out.audio_plan[0]["action"] == "encode"
    assert evaluate(False, ["Preserve Audio"]).preserve_audio is True


def test_queued_job_without_an_audio_choice_preserves_audio(client):
    response = client.post("/api/queue", json={"scope": "show", "preset_id": "show-streaming-efficient-audio",
                                                "media_ids": ["modern-family-s03e04"]})
    job = response.json()["added"][0]
    assert job["preserve_audio"] is True and job["requested_preserve_audio"] is None


@pytest.mark.parametrize("backend", ["cpu", "qsv"])
def test_default_command_never_touches_any_audio_track(probe_facts, tmp_path, backend):
    job = queue_job(qsv_preset(efficient_audio=True), backend="qsv", scope="show") if backend == "qsv" else queue_job()
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/media/Film.mkv"), tmp_path / "out.mkv",
                                          probe_facts, qsv_device=Path("/dev/dri/renderD128"))
    mapped = [command[i + 1] for i, value in enumerate(command) if value == "-map"]
    assert {f"0:{s.index}" for s in probe_facts.streams if s.kind == "audio"} <= set(mapped)
    assert command[command.index("-c") + 1] == "copy"
    assert not [arg for arg in command if re.match(r"-(c|b|ac|filter|af|ar):a", arg)]


def test_command_audio_options_come_from_the_shared_plan(probe_facts, tmp_path):
    job = queue_job(qsv_preset(efficient_audio=True), backend="qsv", scope="show",
                    preserve_audio=False, requested_preserve_audio=False)
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/media/Episode.mkv"), tmp_path / "out.mkv",
                                          probe_facts, qsv_device=Path("/dev/dri/renderD128"))
    plan = plan_audio_tracks([s for s in probe_facts.streams if s.kind == "audio"], job.preset, False)
    for ordinal, track in enumerate(plan):
        if track["action"] == "encode":
            assert command[command.index(f"-c:a:{ordinal}") + 1] == track["codec"]
            assert command[command.index(f"-ac:a:{ordinal}") + 1] == str(track["channels"])
        else:
            assert f"-c:a:{ordinal}" not in command


def test_metadata_opt_out_drops_only_global_tags_not_audio_languages(probe_facts, tmp_path):
    job = queue_job(preserve_subtitles=False)
    command = FFmpegEncoder.build_command("/usr/bin/ffmpeg", job, Path("/media/Film.mkv"), tmp_path / "out.mkv",
                                          probe_facts)
    # A bare "-map_metadata -1" would also wipe every stream's language and title.
    assert "-map_metadata" not in command
    assert command[command.index("-map_metadata:g") + 1] == "-1"


def test_only_copied_tracks_are_hashed_and_mismatches_are_reported(probe_facts, tmp_path, monkeypatch):
    calls = []

    def packet_hashes(self, job_id, path, selectors):
        calls.append(selectors)
        return ["same"] * len(selectors) if len(calls) == 1 else ["different"] * len(selectors)

    monkeypatch.setattr(FFmpegEncoder, "packet_hashes", packet_hashes)
    encoder = FFmpegEncoder("ffmpeg", tmp_path)
    job = queue_job(qsv_preset(efficient_audio=True), backend="qsv", scope="show",
                    preserve_audio=False, requested_preserve_audio=False)
    errors = encoder.copied_audio_mismatches(job, Path("/media/source.mkv"), probe_facts,
                                             tmp_path / "out.mkv", hevc_output(probe_facts))
    # DTS 5.1 is converted and not hashed; the AAC commentary is copied and must match.
    assert calls == [["0:2"], ["0:a:1"]]
    assert errors == ["Copied audio track 2 (aac, por) is not bit-identical to the source."]


def test_packet_hash_report_is_parsed_strictly(tmp_path, monkeypatch):
    class Process:
        def __init__(self, stdout, returncode=0):
            self.stdout, self.returncode = stdout, returncode

        def communicate(self):
            return self.stdout, ""

        def poll(self):
            return self.returncode

    reports = iter([f"0,a,SHA256={'a' * 64}\n1,a,SHA256={'b' * 64}\n", f"0,a,SHA256={'a' * 64}\n"])
    monkeypatch.setattr("app.workers.ffmpeg.subprocess.Popen", lambda *a, **k: Process(next(reports)))
    encoder = FFmpegEncoder("ffmpeg", tmp_path)
    assert encoder.packet_hashes("job", tmp_path / "x.mkv", ["0:1", "0:2"]) == ["a" * 64, "b" * 64]
    with pytest.raises(FFmpegError, match="unexpected hash report"):
        encoder.packet_hashes("job", tmp_path / "x.mkv", ["0:1", "0:2"])


@pytest.mark.parametrize("change, message", [
    ({"language": "spa"}, "language is spa; expected eng"),
    ({"language": None}, "language is missing"),
    ({"title": None}, "title changed or is missing"),
    ({"codec": "ac3"}, "codec is ac3, expected dts"),
    ({"channels": 2}, "has 2 channels; expected 6"),
    ({"sample_rate": 44100}, "sample rate changed"),
])
def test_cpu_validation_checks_every_audio_track(probe_facts, tmp_path, change, message):
    output = hevc_output(probe_facts)
    output = output.model_copy(update={"streams": [
        stream.model_copy(update=change) if stream.index == 1 else stream for stream in output.streams]})
    output_file = tmp_path / "out.mkv"
    output_file.write_bytes(b"x")
    errors = CPUEncodeCapability().validate_output(movie_item(probe_facts), queue_job(), output, output_file)
    assert any(message in error for error in errors), errors


# --- Default lane in the compression modal -----------------------------------------------


def test_show_rows_suggest_an_eligible_gpu_preset_and_movies_keep_cpu(client, catalog):
    episode = catalog.find("modern-family-s03e04", "show")
    preset, result = catalog.preview(episode)
    assert preset is not None and preset.backend == "qsv" and result.eligible
    assert preset.id == "show-streaming-quality"  # Audio-preserving GPU preset before Efficient Audio.
    movie_preset, _ = catalog.preview(catalog.find("movie-king-of-comedy", "movie"))
    assert movie_preset is not None and movie_preset.backend == "cpu"
    client.patch("/api/tags", json={"targets": [{"kind": "episode", "id": "modern-family-s03e04"}],
                                    "tags": ["Quality CPU"], "operation": "replace"})
    preset, result = catalog.preview(catalog.find("modern-family-s03e04", "show"))
    assert preset is not None and preset.backend == "cpu" and result.eligible


# --- Optional: the real ffmpeg on this machine (runs in the CT) ---------------------------

FFMPEG = os.environ.get("KOMPRESSOR_TEST_FFMPEG") or shutil.which("ffmpeg")


def has_encoder(name: str) -> bool:
    if not FFMPEG:
        return False
    listing = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return f" {name} " in listing


@pytest.mark.skipif(not has_encoder("libx265"), reason="ffmpeg with libx265 is optional and not installed")
@pytest.mark.parametrize("preserve_metadata", [True, False])
def test_real_cpu_command_copies_every_audio_track_bit_exact(tmp_path, preserve_metadata):
    assert FFMPEG is not None
    ffmpeg: str = FFMPEG
    source = tmp_path / "source.mkv"
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000",
                    "-f", "lavfi", "-i", "sine=f=220:sample_rate=48000", "-t", "3",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "mpeg4", "-pix_fmt", "yuv420p",
                    "-c:a:0", "aac", "-ac:a:0", "2", "-c:a:1", "ac3", "-ac:a:1", "6",
                    "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=Stereo",
                    "-metadata:s:a:1", "language=por", "-metadata:s:a:1", "title=Surround",
                    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                    str(source)], check=True)
    sdr = HDRSignalling(transfer="bt709", primaries="bt709", matrix="bt709", bit_depth=8)
    probe = MediaProbeResult(container="matroska,webm", duration_seconds=3.0, streams=[
        StreamFacts(index=0, kind="video", codec="mpeg4", width=320, height=240, pixel_format="yuv420p",
                    scan_type="progressive", hdr=sdr),
        StreamFacts(index=1, kind="audio", codec="aac", channels=2, language="eng", title="Stereo"),
        StreamFacts(index=2, kind="audio", codec="ac3", channels=6, bitrate=448_000, language="por",
                    title="Surround"),
    ])
    job = queue_job(preserve_subtitles=preserve_metadata,
                    preset=queue_job().preset.model_copy(update={"encoder_preset": "fast"}))
    output = tmp_path / "out.mkv"
    command = FFmpegEncoder.build_command(ffmpeg, job, source, output, probe)
    subprocess.run(command, check=True, capture_output=True)
    encoder = FFmpegEncoder(ffmpeg, tmp_path)
    output_probe = probe.model_copy(update={"streams": [
        s.model_copy(update={"codec": "hevc"}) if s.kind == "video" else s for s in probe.streams]})
    assert encoder.copied_audio_mismatches(job, source, probe, output, output_probe) == []
    info = subprocess.run([ffmpeg, "-hide_banner", "-i", str(output)], capture_output=True, text=True).stderr
    assert re.search(r"Stream #0:1\(eng\): Audio: aac", info) and re.search(r"Stream #0:2\(por\): Audio: ac3", info)
    assert "Surround" in info and "Stereo" in info

    # A re-encoded track must be caught.
    tampered = tmp_path / "tampered.mkv"
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(output), "-map", "0",
                    "-c", "copy", "-c:a:0", "aac", "-b:a:0", "96k", str(tampered)], check=True)
    errors = encoder.copied_audio_mismatches(job, source, probe, tampered, output_probe)
    assert errors == ["Copied audio track 1 (aac, eng) is not bit-identical to the source."]
