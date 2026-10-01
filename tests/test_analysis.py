"""History quality tools: side-by-side comparison screenshots."""
import json
import random
import re
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models.probe import HDRSignalling, StreamFacts
from app.services.analysis import (AnalysisService, AnalysisTarget, comparison_timestamps, half_frame,
                                   parse_first_frame, screenshot_command, seek, summarize_vmaf, vmaf_command)
from app.services.errors import Conflict
from tests.test_real_encoding import build_real_app, probe_facts  # noqa: F401
from tests.test_source_replacement import FFMPEG, has_encoder, hevc_output, run_one


def video(**changes) -> StreamFacts:
    values = dict(index=0, kind="video", codec="h264", profile="High", width=1920, height=1080,
                  pixel_format="yuv420p", scan_type="progressive",
                  hdr=HDRSignalling(transfer="bt709", primaries="bt709", matrix="bt709", bit_depth=8))
    values.update(changes)
    return StreamFacts.model_validate(values)


def wait_until_idle(service, timeout=5.0):
    deadline = time.monotonic() + timeout
    while service.status()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    return service.status()


def test_timestamps_are_sorted_and_skip_the_first_and_last_five_percent():
    stamps = comparison_timestamps(1000, 50, random.Random(7))
    assert stamps == sorted(stamps) and len(stamps) == 50
    assert all(50 <= stamp <= 950 for stamp in stamps)


def test_seeks_are_absolute_from_each_files_first_frame_minus_half_a_frame():
    stream = video(frame_rate="25")
    assert half_frame(stream) == 0.02
    assert seek(0.0, 2.0, stream) == ["-seek_timestamp", "1", "-ss", "1.980000"]
    assert seek(0.04, 2.0, stream) == ["-seek_timestamp", "1", "-ss", "2.020000"]
    assert parse_first_frame("[Parsed_showinfo_0 @ 0x1] n:   0 pts:     40 pts_time:0.04  duration:40") == 0.04


def test_screenshot_command_stacks_source_left_output_right_at_the_same_frame():
    command = screenshot_command("ffmpeg", Path("/media/Film.mkv"), Path("/ws/out.mkv"), video(index=2),
                                 seek(0.0, 61.5, video()), seek(0.04, 61.5, video()), Path("/ws/compare/01.png"))
    assert [command[i + 1] for i, value in enumerate(command) if value == "-ss"] == ["61.480000", "61.520000"]
    assert command.count("-seek_timestamp") == 2
    inputs = [command[i + 1] for i, value in enumerate(command) if value == "-i"]
    assert inputs == ["/media/Film.mkv", "/ws/out.mkv"]
    graph = command[command.index("-filter_complex") + 1]
    assert graph.startswith("[0:2]scale=1920:1080") and "[1:v:0]scale=1920:1080" in graph
    assert graph.endswith("[source][output]hstack=inputs=2") and "setparams" not in graph
    assert command[-3:] == ["-frames:v", "1", "/ws/compare/01.png"]


def test_untagged_sources_get_the_same_assumed_colours_the_encoder_wrote():
    untagged = video(hdr=HDRSignalling(bit_depth=8))
    graph = screenshot_command("ffmpeg", Path("s.mkv"), Path("o.mkv"), untagged, [], [], Path("x.png"))[-4]
    assert graph.startswith("[0:0]setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,scale=")


def fake_run(command, progress=None):
    """First-frame probes report t=0; image commands write their file."""
    if "showinfo" in command:
        return subprocess.CompletedProcess(command, 0, "", "pts_time:0")
    Path(command[-1]).write_bytes(b"png")
    return subprocess.CompletedProcess(command, 0, "", "")


def service(tmp_path, recorded, run=None):
    target = AnalysisTarget(job_id="job-12345678", name="Show · S01E01", source=tmp_path / "s.mkv",
                            output=tmp_path / "o.mkv", duration=600, video=video())
    analysis = AnalysisService("ffmpeg", tmp_path / "compare", lambda job_id: target,
                               lambda job_id, kind, result: recorded.append((job_id, kind, result)))
    if run is not None:
        analysis._run = run
    return analysis


def test_compare_writes_screenshots_into_the_compare_folder_and_records_them(tmp_path):
    recorded = []
    analysis = service(tmp_path, recorded, fake_run)
    assert analysis.start_compare("job-12345678", 3)["state"] == "running"
    status = wait_until_idle(analysis)
    assert status["state"] == "completed", status
    folder = Path(status["result"]["folder"])
    assert folder.parent == tmp_path / "compare" / "Show_S01E01-job-1234"
    assert sorted(p.name for p in folder.iterdir()) == status["result"]["files"] and len(status["result"]["files"]) == 3
    assert all(re.fullmatch(r"\d\d_\d\dh\d\dm\d\d\.\d{3}s_source-left_output-right\.png", name)
               for name in status["result"]["files"])
    assert recorded == [("job-12345678", "comparison", status["result"])]


def test_only_one_analysis_runs_at_a_time_and_failures_are_reported(tmp_path):
    release = []

    def run(command, progress=None):
        while not release:
            time.sleep(0.01)
        raise RuntimeError("ffmpeg failed: boom")

    analysis = service(tmp_path, [], run)
    analysis.start_compare("job-12345678", 1)
    with pytest.raises(Conflict, match="Another comparison"):
        analysis.start_compare("job-12345678", 1)
    release.append(True)
    status = wait_until_idle(analysis)
    assert status["state"] == "failed" and "boom" in status["error"]
    analysis._run = fake_run
    analysis.start_compare("job-12345678", 1)  # Not busy any more.
    assert wait_until_idle(analysis)["state"] == "completed"


def test_compare_endpoint_uses_the_kept_output_and_saves_the_result_on_the_job(
        tmp_path, monkeypatch, probe_facts):
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)
    commands = []

    def run(self, command, progress=None):
        commands.append(command)
        return fake_run(command)

    monkeypatch.setattr(AnalysisService, "_run", run)
    monkeypatch.setattr(AnalysisService, "vmaf_available", lambda self: True)
    with TestClient(application) as client:
        processor = application.state.media_processor
        job = run_one(processor, replace_source=False)
        response = client.post(f"/api/queue/{job['id']}/compare", json={"count": 2})
        assert response.status_code == 202, response.text
        status = wait_until_idle(processor.analysis)
        assert status["state"] == "completed"
        shots = [command for command in commands if "hstack" in " ".join(command)]
        inputs = [value for command in shots for i, value in enumerate(command) if command[i - 1] == "-i"]
        assert inputs == [str(source), job["output_path"]] * 2
        assert Path(status["result"]["folder"]).is_relative_to(workspace / "compare")
        saved = next(item for item in client.get("/api/queue").json()["history"] if item["id"] == job["id"])
        assert saved["comparison"]["files"] == status["result"]["files"]
        assert client.get("/api/queue/analysis/status").json()["state"] == "completed"


def test_compare_is_refused_when_the_source_no_longer_matches_the_output(tmp_path, monkeypatch, probe_facts):
    application, source, _, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)
    with TestClient(application) as client:
        processor = application.state.media_processor
        job = run_one(processor, replace_source=False)
        with source.open("ab") as handle:
            handle.write(b"changed")
        processor.scan_library()
        response = client.post(f"/api/queue/{job['id']}/compare")
        assert response.status_code == 409 and "source changed" in response.json()["detail"]


def test_compare_is_refused_for_replaced_jobs_and_in_seed_mode(tmp_path, monkeypatch, probe_facts, client):
    added = client.post("/api/queue", json={"scope": "movie", "preset_id": "movie-streaming-quality",
                                           "media_ids": ["movie-king-of-comedy"]}).json()["added"][0]
    assert client.post(f"/api/queue/{added['id']}/compare").status_code == 422
    application, _, _, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)
    with TestClient(application) as real:
        job = run_one(application.state.media_processor, replace_source=True)
        assert job["source_replaced"]
        assert real.post(f"/api/queue/{job['id']}/compare").status_code == 409


@pytest.mark.skipif(not has_encoder("mpeg4"), reason="ffmpeg is optional and not installed")
def test_real_ffmpeg_writes_a_double_width_side_by_side_png(tmp_path):
    assert FFMPEG is not None
    ffmpeg: str = FFMPEG
    source, output, png = tmp_path / "source.mkv", tmp_path / "output.mkv", tmp_path / "shot.png"
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-t", "4", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(source)], check=True)
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(source), "-c:v", "mpeg4", "-q:v", "20",
                    str(output)], check=True)
    stream = video(codec="mpeg4", width=320, height=240, frame_rate="25")
    command = screenshot_command(ffmpeg, source, output, stream, seek(0, 2.0, stream), seek(0, 2.0, stream), png)
    subprocess.run(command, check=True)
    info = subprocess.run([ffmpeg, "-hide_banner", "-i", str(png)], capture_output=True, text=True).stderr
    assert re.search(r"Video: png, rgb24.*640x240", info), info


# --- VMAF benchmark -----------------------------------------------------------------------


def test_vmaf_command_scores_the_output_against_the_source_segment(tmp_path):
    log = tmp_path / "vmaf.json"
    command = vmaf_command("ffmpeg", Path("/media/Film.mkv"), Path("/ws/out.mkv"), video(index=3),
                           seek(0.0, 100, video()), seek(0.04, 100, video()), 60, log, 4)
    inputs = [command[i + 1] for i, value in enumerate(command) if value == "-i"]
    assert inputs == ["/ws/out.mkv", "/media/Film.mkv"]  # libvmaf: distorted first, reference second
    assert command.count("-t") == 2 and command[command.index("-t") + 1] == "60.000"
    graph = command[command.index("-lavfi") + 1]
    assert "[1:3]setpts=PTS-STARTPTS,format=yuv420p10le[reference]" in graph
    assert f"log_path='{log}'" in graph and "n_threads=4" in graph and "vmaf_4k" not in graph
    uhd = vmaf_command("ffmpeg", Path("s"), Path("o"), video(width=3840, height=2160), [], [], 10, log, 1)
    assert "model='version=vmaf_4k_v0.6.1'" in uhd[uhd.index("-lavfi") + 1]
    with pytest.raises(Exception, match="unsafe"):
        vmaf_command("ffmpeg", Path("s"), Path("o"), video(), [], [], 10, tmp_path / "a:b.json", 1)


def test_vmaf_summary_reports_mean_lows_and_minimum():
    frames = [{"frameNum": i, "metrics": {"vmaf": float(score)}} for i, score in enumerate(range(1, 101))]
    summary = summarize_vmaf({"frames": frames, "pooled_metrics": {"vmaf": {"mean": 50.5, "harmonic_mean": 19.28}}})
    assert summary == {"mean": 50.5, "harmonic_mean": 19.28, "min": 1.0, "p1": 2.0, "p5": 6.0, "frames": 100}


def test_benchmark_runs_a_centred_segment_and_records_the_score(tmp_path):
    recorded = []
    commands = []

    def run(command, progress=None):
        commands.append(command)
        if "showinfo" in command:
            return subprocess.CompletedProcess(command, 0, "", "pts_time:0")
        graph = command[command.index("-lavfi") + 1]
        match = re.search(r"log_path='([^']+)'", graph)
        assert match is not None
        log = Path(match[1])
        log.write_text(json.dumps({"frames": [{"metrics": {"vmaf": 95.0}}, {"metrics": {"vmaf": 91.0}}],
                                   "pooled_metrics": {"vmaf": {"mean": 93.0, "harmonic_mean": 92.9}}}))
        assert progress is not None
        progress("out_time_us", "30000000")
        return subprocess.CompletedProcess(command, 0, "", "")

    analysis = service(tmp_path, recorded, run)
    analysis._vmaf_available = True
    analysis.start_benchmark("job-12345678", 60)
    status = wait_until_idle(analysis)
    assert status["state"] == "completed", status
    result = status["result"]
    assert (result["mean"], result["min"], result["frames"]) == (93.0, 91.0, 2)
    assert (result["start_seconds"], result["duration_seconds"]) == (270.0, 60.0)  # middle of 600 s
    vmaf = next(command for command in commands if "-lavfi" in command)
    assert [vmaf[i + 1] for i, value in enumerate(vmaf) if value == "-ss"] == ["269.980000", "269.980000"]
    assert recorded[0][1] == "vmaf" and Path(result["log"]).parent.name.startswith("vmaf-")


def test_benchmark_validates_duration_and_requires_libvmaf(tmp_path):
    analysis = service(tmp_path, [])
    with pytest.raises(Exception, match="between 5 and 600"):
        analysis.start_benchmark("job-12345678", 3)
    analysis._vmaf_available = False
    with pytest.raises(Exception, match="no libvmaf"):
        analysis.start_benchmark("job-12345678", 60)


def test_benchmark_endpoint_saves_the_score_on_the_history_job(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)

    def run(self, command, progress=None):
        if "showinfo" in command:
            return subprocess.CompletedProcess(command, 0, "", "pts_time:0")
        match = re.search(r"log_path='([^']+)'", command[command.index("-lavfi") + 1])
        assert match is not None
        log = Path(match[1])
        log.write_text(json.dumps({"frames": [{"metrics": {"vmaf": 96.0}}], "pooled_metrics": {"vmaf": {"mean": 96.0}}}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(AnalysisService, "_run", run)
    monkeypatch.setattr(AnalysisService, "vmaf_available", lambda self: True)
    with TestClient(application) as client:
        processor = application.state.media_processor
        job = run_one(processor, replace_source=False)
        assert client.post(f"/api/queue/{job['id']}/benchmark", json={"seconds": 2}).status_code == 422
        response = client.post(f"/api/queue/{job['id']}/benchmark", json={"seconds": 30})
        assert response.status_code == 202, response.text
        assert wait_until_idle(processor.analysis)["state"] == "completed"
        saved = next(item for item in client.get("/api/queue").json()["history"] if item["id"] == job["id"])
        assert saved["vmaf"]["mean"] == 96.0 and saved["vmaf"]["duration_seconds"] == 30


def has_filter(name: str) -> bool:
    if not FFMPEG:
        return False
    return f" {name} " in subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True).stdout


@pytest.mark.skipif(not (has_filter("libvmaf") and has_encoder("libx265")),
                    reason="ffmpeg with libvmaf and libx265 is optional and not installed")
def test_real_benchmark_aligns_frames_across_different_start_offsets(tmp_path):
    """AAC priming gives the source a -0.021 s container start; the HEVC output starts
    at 0 with its first frame at 0.04 s. Container-time seeking compares frames one
    apart; first-frame alignment must score the near-lossless output as such."""
    assert FFMPEG is not None
    ffmpeg: str = FFMPEG
    source, output = tmp_path / "source.mkv", tmp_path / "output.mkv"
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000", "-t", "9", "-map", "0", "-map", "1",
                    "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(source), "-map", "0", "-c", "copy",
                    "-c:v:0", "libx265", "-crf:v:0", "12", "-preset:v:0", "ultrafast", "-x265-params", "log-level=none",
                    str(output)], check=True)
    stream = video(codec="mpeg4", width=320, height=240, frame_rate="25", hdr=HDRSignalling(bit_depth=8))
    target = AnalysisTarget(job_id="job-real-vmaf", name="Real", source=source, output=output,
                            duration=9.0, video=stream)
    analysis = AnalysisService(ffmpeg, tmp_path / "compare", lambda job_id: target, lambda *args: None)
    # A 5 s segment of 9 s starts at 2.0 s, where container-time seeking lands one
    # frame apart in these two files (verified: ~40 instead of ~99).
    analysis.start_benchmark("job-real-vmaf", 5)
    status = wait_until_idle(analysis, timeout=120)
    assert status["state"] == "completed", status
    assert status["result"]["start_seconds"] == 2.0
    assert status["result"]["mean"] > 90, status["result"]
