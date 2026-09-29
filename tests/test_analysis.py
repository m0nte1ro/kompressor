"""History quality tools: side-by-side comparison screenshots."""
import random
import re
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models.probe import HDRSignalling, StreamFacts
from app.services.analysis import AnalysisService, AnalysisTarget, comparison_timestamps, screenshot_command
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


def test_screenshot_command_stacks_source_left_output_right_at_the_same_time():
    command = screenshot_command("ffmpeg", Path("/media/Film.mkv"), Path("/ws/out.mkv"), video(index=2), 61.5,
                                 Path("/ws/compare/01.png"))
    assert command[command.index("-ss") + 1] == "61.500" and command.count("-ss") == 2
    inputs = [command[i + 1] for i, value in enumerate(command) if value == "-i"]
    assert inputs == ["/media/Film.mkv", "/ws/out.mkv"]
    graph = command[command.index("-filter_complex") + 1]
    assert graph.startswith("[0:2]scale=1920:1080") and "[1:v:0]scale=1920:1080" in graph
    assert graph.endswith("[source][output]hstack=inputs=2") and "setparams" not in graph
    assert command[-3:] == ["-frames:v", "1", "/ws/compare/01.png"]


def test_untagged_sources_get_the_same_assumed_colours_the_encoder_wrote():
    untagged = video(hdr=HDRSignalling(bit_depth=8))
    graph = screenshot_command("ffmpeg", Path("s.mkv"), Path("o.mkv"), untagged, 1, Path("x.png"))[-4]
    assert graph.startswith("[0:0]setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,scale=")


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

    def run(command):
        Path(command[-1]).write_bytes(b"png")

    analysis = service(tmp_path, recorded, run)
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

    def run(command):
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
    analysis._run = lambda command: Path(command[-1]).write_bytes(b"png")
    analysis.start_compare("job-12345678", 1)  # Not busy any more.
    assert wait_until_idle(analysis)["state"] == "completed"


def test_compare_endpoint_uses_the_kept_output_and_saves_the_result_on_the_job(
        tmp_path, monkeypatch, probe_facts):
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), output_size=1_000_000)
    commands = []

    def run(self, command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"png")

    monkeypatch.setattr(AnalysisService, "_run", run)
    with TestClient(application) as client:
        processor = application.state.media_processor
        job = run_one(processor, replace_source=False)
        response = client.post(f"/api/queue/{job['id']}/compare", json={"count": 2})
        assert response.status_code == 202, response.text
        status = wait_until_idle(processor.analysis)
        assert status["state"] == "completed"
        inputs = [value for command in commands for i, value in enumerate(command) if command[i - 1] == "-i"]
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
    command = screenshot_command(ffmpeg, source, output, video(codec="mpeg4", width=320, height=240), 2.0, png)
    subprocess.run(command, check=True)
    info = subprocess.run([ffmpeg, "-hide_banner", "-i", str(png)], capture_output=True, text=True).stderr
    assert re.search(r"Video: png, rgb24.*640x240", info), info
