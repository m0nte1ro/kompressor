"""History "Replace source": swapping a kept keep-original output into its source."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.errors import Conflict
from app.workers.ffmpeg import FFmpegEncoder
from tests.test_real_encoding import build_real_app, probe_facts  # noqa: F401
from tests.test_source_replacement import hevc_output, run_one


def execute(processor, job_id):
    job = processor.queue.claim_next("cpu")
    assert job is not None and job.id == job_id
    worker = processor.queue.worker
    try:
        worker._execute(job)
    except Exception as error:
        processor.queue.fail_real_job(job.id, str(error))
        worker.encoder.cleanup(job.id, remove_final=True)
    finally:
        worker.encoder.cleanup(job.id)
    return processor.queue._find(job.id)


def kept(tmp_path, monkeypatch, probe_facts, **options):
    application, source, workspace, _ = build_real_app(
        tmp_path, monkeypatch, probe_facts, hevc_output(probe_facts), **options)
    return application, source, workspace


def test_history_replace_swaps_the_kept_output_in_and_consumes_it(tmp_path, monkeypatch, probe_facts):
    application, source, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=1_000_000)
    hashed = []
    monkeypatch.setattr(FFmpegEncoder, "packet_hashes", lambda self, job_id, path, selectors: (
        hashed.append(Path(path).name) or [f"same-{i}" for i in range(len(selectors))]))
    with TestClient(application) as client:
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        output = Path(original["output_path"])
        assert original["status"] == "completed" and output.is_file()
        response = client.post(f"/api/queue/{original['id']}/replace-source")
        assert response.status_code == 201, response.text
        replacement = response.json()
        assert replacement["replaces_job_id"] == original["id"] and replacement["reuse_output_path"] == str(output)
        hashed.clear()
        done = execute(processor, replacement["id"])
        assert done.status == "completed" and done.source_replaced, done
        # The kept output is re-validated, including the bit-exact audio check.
        assert hashed == ["Fixture.mkv", "Fixture.kompressor.mkv"]
        assert source.stat().st_size == 1_000_000 and not output.exists()
        assert sorted(source.parent.iterdir()) == [source]
        kept_job = processor.queue._find(original["id"])
        assert kept_job.output_path is None and "moved into the source" in kept_job.reasons[-1]


def test_history_replace_ignores_the_preset_minimum_but_never_replaces_with_a_larger_file(
        tmp_path, monkeypatch, probe_facts):
    # 2.5% saving: below the 20% preset minimum, but an explicit History choice.
    application, source, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=390_000_000)
    with TestClient(application):
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        replacement = processor.replace_with_kept_output(original["id"])
        assert execute(processor, replacement["id"]).source_replaced
        assert source.stat().st_size == 390_000_000


def test_history_replace_refuses_a_larger_output_and_keeps_both_files(tmp_path, monkeypatch, probe_facts):
    application, source, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=500_000_000)
    with TestClient(application):
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        replacement = processor.replace_with_kept_output(original["id"])
        done = execute(processor, replacement["id"])
        assert done.status == "skipped" and not done.source_replaced
        assert "not smaller than the source" in done.reasons[-1]
        assert source.stat().st_size == 400_000_000
        assert Path(original["output_path"]).is_file()
        assert processor.queue._find(original["id"]).output_path == original["output_path"]


def test_history_replace_fails_safely_when_the_source_changed_after_encoding(
        tmp_path, monkeypatch, probe_facts):
    application, source, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=1_000_000)
    with TestClient(application):
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        replacement = processor.replace_with_kept_output(original["id"])
        os.utime(source, ns=(1, 1))  # e.g. Sonarr upgraded the file in place meanwhile
        done = execute(processor, replacement["id"])
        assert done.status == "failed" and not done.source_replaced
        assert "changed" in (done.error_message or ""), done.error_message
        assert source.stat().st_size == 400_000_000
        assert Path(original["output_path"]).is_file()  # The kept output survives a failed attempt.


def test_history_replace_is_refused_for_ineligible_jobs(tmp_path, monkeypatch, probe_facts):
    application, _, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=1_000_000)
    with TestClient(application) as client:
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        processor.replace_with_kept_output(original["id"])
        with pytest.raises(Conflict, match="already has a queued or active job"):
            processor.replace_with_kept_output(original["id"])
        assert client.post("/api/queue/unknown/replace-source").status_code == 404


def test_history_replace_refuses_outputs_that_left_the_workspace(tmp_path, monkeypatch, probe_facts):
    application, _, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=1_000_000)
    with TestClient(application):
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        replacement = processor.replace_with_kept_output(original["id"])
        Path(original["output_path"]).unlink()
        done = execute(processor, replacement["id"])
        assert done.status == "failed" and "no longer in the workspace" in (done.error_message or "")


def test_kept_output_path_must_belong_to_that_job(tmp_path):
    encoder = FFmpegEncoder("ffmpeg", tmp_path)
    (tmp_path / "jobs" / "job-a").mkdir(parents=True)
    output = tmp_path / "jobs" / "job-a" / "Film.kompressor.mkv"
    output.write_bytes(b"x")
    assert encoder.kept_output("job-a", str(output)) == output
    for job_id, path in [("job-b", output), ("job-a", tmp_path / "jobs" / "job-a" / "Film.kompressor.partial.mkv"),
                         ("job-a", tmp_path / "elsewhere.kompressor.mkv")]:
        with pytest.raises(Exception, match="not a Kompressor workspace output|no longer"):
            encoder.kept_output(job_id, str(path))


def test_seed_mode_refuses_history_replacement(client):
    added = client.post("/api/queue", json={"scope": "movie", "preset_id": "movie-streaming-quality",
                                           "media_ids": ["movie-king-of-comedy"]}).json()["added"][0]
    assert client.post(f"/api/queue/{added['id']}/replace-source").status_code == 422



def test_deleting_a_history_entry_removes_its_kept_output_but_never_the_source(
        tmp_path, monkeypatch, probe_facts):
    application, source, _ = kept(tmp_path, monkeypatch, probe_facts, output_size=1_000_000)
    with TestClient(application) as client:
        processor = application.state.media_processor
        original = run_one(processor, replace_source=False)
        output = Path(original["output_path"])
        before = source.stat()

        replacement = processor.replace_with_kept_output(original["id"])
        refused = client.delete(f"/api/queue/history/{original['id']}")
        assert refused.status_code == 409 and "replacement" in refused.json()["detail"]
        assert client.delete(f"/api/queue/history/{replacement['id']}").status_code == 409
        processor.remove_queued_job(replacement["id"])

        assert client.delete(f"/api/queue/history/{original['id']}").status_code == 204
        assert all(job["id"] != original["id"] for job in client.get("/api/queue").json()["history"])
        assert not output.exists()
        assert source.stat().st_ino == before.st_ino and source.stat().st_size == before.st_size
