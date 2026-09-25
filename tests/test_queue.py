from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.models.queue import EnqueueRequest
from app.models.preferences import WorkerLaneSettings, WorkerSettings


def add(client, payload):
    response = client.post("/api/queue", json=payload)
    assert response.status_code == 201
    return response.json()


def test_bulk_skips_blocked_missing_and_duplicates(client, movie_payload):
    movie_payload["media_ids"] = ["movie-dune-part-two", "movie-hardlinked-example", "movie-king-of-comedy", "movie-king-of-comedy", "missing"]
    result = add(client, movie_payload)
    assert len(result["added"]) == 1
    assert len(result["excluded"]) == 3
    again = add(client, movie_payload)
    assert not again["added"]
    assert any("Already queued" in reason for item in again["excluded"] for reason in item["reasons"])
    assert client.get("/api/queue").json()["pending_count"] == 1


def test_cross_scope_is_rejected_by_backend(client, movie_payload, show_payload):
    movie_payload["preset_id"] = "show-streaming-quality"
    show_payload["preset_id"] = "movie-streaming-quality"
    for payload in (movie_payload, show_payload):
        result = add(client, payload)
        assert not result["added"]
        assert any("scope" in reason for reason in result["excluded"][0]["reasons"])


def test_preservation_and_preset_snapshot(client, catalog, show_payload):
    catalog.media.library.shows[0].tags.append("Preserve Audio")
    show_payload.update(preserve_audio=False, preserve_subtitles=False)
    job = add(client, show_payload)["added"][0]
    assert job["preserve_audio"] is True
    assert job["preserve_subtitles"] is False
    assert job["preset"]["rate_control"] == "icq"
    assert job["preset"]["quality_value"] == 23
    assert job["backend"] == "qsv"
    assert client.post("/api/queue", json={**show_payload, "target_video_bitrate": 1}).status_code == 422
    assert client.post("/api/queue", json={**show_payload, "backend": "cpu"}).status_code == 422


def test_dual_lane_progress_and_active_delete_protection(client, queue, movie_payload, show_payload):
    cpu = add(client, movie_payload)["added"][0]
    qsv = add(client, show_payload)["added"][0]
    queue.tick(0)
    queue.tick(90)
    lanes = client.get("/api/queue").json()["lanes"]
    assert {lane["active"]["id"] for lane in lanes} == {cpu["id"], qsv["id"]}
    assert all(lane["active"]["progress"] == 50 for lane in lanes)
    for job in (cpu, qsv):
        assert client.delete(f"/api/queue/{job['id']}").status_code == 409
        assert client.post(f"/api/queue/{job['id']}/move-next").status_code == 409
        assert client.patch(f"/api/queue/{job['id']}/priority", json={"priority": "urgent"}).status_code == 409
    queue.tick(90)
    assert all(lane["active"]["status"] == "validating" for lane in client.get("/api/queue").json()["lanes"])
    assert client.delete(f"/api/queue/{cpu['id']}").status_code == 409
    queue.tick(5)
    state = client.get("/api/queue").json()
    assert state["pending_count"] == 0
    assert len(state["history"]) == 2
    assert all(job["status"] == "completed" and job["finished_at"] for job in state["history"])
    assert catalog_movie_size(queue) == cpu["source_size"]


def catalog_movie_size(queue):
    return queue.catalog.find("movie-king-of-comedy", "movie").item.size


def test_order_priority_move_next_and_skip(client, queue, catalog, movie_payload):
    original = catalog.media.library.movies[1]
    for index, bitrate in enumerate([18_000_000, 28_000_000]):
        catalog.media.library.movies.append(original.model_copy(update={"id": f"copy-{index}", "video_bitrate": bitrate, "size": 30_000_000_000}))
    movie_payload["media_ids"] = ["copy-0", original.id, "copy-1"]
    add(client, movie_payload)
    queued = client.get("/api/queue").json()["lanes"][0]["queued"]
    assert [j["planning_saving"] for j in queued] == sorted((j["planning_saving"] for j in queued), reverse=True)
    smallest = queued[-1]
    assert client.patch(f"/api/queue/{smallest['id']}/priority", json={"priority": "urgent"}).status_code == 204
    assert client.get("/api/queue").json()["lanes"][0]["queued"][0]["id"] == smallest["id"]
    next_job = queued[1]
    assert client.post(f"/api/queue/{next_job['id']}/move-next").status_code == 204
    assert client.get("/api/queue").json()["lanes"][0]["queued"][0]["id"] == next_job["id"]
    queue.tick(0)
    assert client.post(f"/api/queue/{next_job['id']}/skip").status_code == 204
    state = client.get("/api/queue").json()
    assert state["history"][0]["status"] == "skipped"
    assert state["lanes"][0]["active"]["id"] == smallest["id"]
    assert client.post(f"/api/queue/{next_job['id']}/skip").status_code == 409


def test_move_next_persists_and_new_override_wins(client, movie_payload, catalog):
    movie = catalog.media.library.movies[1]
    catalog.media.library.movies.append(movie.model_copy(update={"id": "copy"}))
    movie_payload["media_ids"].append("copy")
    jobs = add(client, movie_payload)["added"]
    for job in jobs:
        client.post(f"/api/queue/{job['id']}/move-next")
    for _ in range(3):
        state = client.get("/api/queue").json()
        assert state["lanes"][0]["queued"][0]["id"] == jobs[1]["id"]
    client.patch(f"/api/queue/{jobs[1]['id']}/priority", json={"priority": "normal"})
    assert client.get("/api/queue").json()["lanes"][0]["queued"][0]["id"] == jobs[0]["id"]


def test_remove_and_invalid_actions(client, movie_payload):
    job = add(client, movie_payload)["added"][0]
    assert client.post(f"/api/queue/{job['id']}/skip").status_code == 409
    assert client.patch(f"/api/queue/{job['id']}/priority", json={"priority": "super"}).status_code == 422
    assert client.delete(f"/api/queue/{job['id']}").status_code == 204
    assert client.delete(f"/api/queue/{job['id']}").status_code == 404
    assert client.get("/api/queue").json()["pending_count"] == 0
    assert client.post("/api/queue", json={**movie_payload, "preset_id": "missing"}).status_code == 404
    assert client.post("/api/queue", json={**movie_payload, "media_ids": []}).status_code == 422
    assert client.post("/api/queue", json={**movie_payload, "media_ids": ["a"] * 501}).status_code == 422
    assert client.post("/api/queue/missing/skip").status_code == 404


def test_concurrent_enqueue_is_atomic(queue, movie_payload):
    request = EnqueueRequest.model_validate(movie_payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: queue.enqueue(request), range(12)))
    assert sum(len(result["added"]) for result in results) == 1
    assert queue.snapshot()["pending_count"] == 1


@pytest.mark.parametrize("preset_change,reason", [({"enabled": False}, "disabled"), ({"destination_codec": "av1"}, "AV1")])
def test_unavailable_presets_cannot_enqueue(queue, catalog, movie_payload, monkeypatch, preset_change, reason):
    preset = catalog.preset(movie_payload["preset_id"]).model_copy(update=preset_change)
    monkeypatch.setattr(catalog.presets, "get_by_id", lambda _: preset)
    result = queue.enqueue(EnqueueRequest.model_validate(movie_payload))
    assert not result["added"]
    assert any(reason in r for r in result["excluded"][0]["reasons"])


def test_seed_worker_blocks_persisted_real_jobs_instead_of_simulating(queue, movie_payload):
    job = queue.enqueue(EnqueueRequest.model_validate(movie_payload))["added"][0]
    saved = next(item for item in queue.repository.get_all() if item.id == job["id"])
    saved.execution_mode = "real"
    queue.repository.save(saved)
    queue.recover()
    blocked = queue._find(saved.id)
    assert blocked.status == "blocked"
    assert "cannot run in fake mode" in blocked.reasons[0]
    queue.tick(10_000)
    assert queue._find(saved.id).status == "blocked"


def test_manual_worker_pause_prevents_claim_until_resumed(client, queue, movie_payload):
    assert client.post("/api/queue/workers/cpu/pause").status_code == 200
    job = add(client, movie_payload)["added"][0]
    queue.tick(0)
    cpu = next(lane for lane in client.get("/api/queue").json()["lanes"] if lane["backend"] == "cpu")
    assert cpu["active"] is None
    assert cpu["queued"][0]["id"] == job["id"]
    assert client.get("/api/queue/workers").json()["lanes"]["cpu"]["paused"] is True

    assert client.post("/api/queue/workers/cpu/resume").status_code == 200
    queue.tick(0)
    cpu = next(lane for lane in client.get("/api/queue").json()["lanes"] if lane["backend"] == "cpu")
    assert cpu["active"]["id"] == job["id"]


def test_pause_all_can_be_resumed_through_api(client, queue, movie_payload):
    paused = client.post("/api/queue/workers/pause-all")
    assert paused.status_code == 200
    assert all(lane["paused"] for lane in paused.json()["lanes"].values())

    job = add(client, movie_payload)["added"][0]
    queue.tick(0)
    cpu = next(lane for lane in client.get("/api/queue").json()["lanes"] if lane["backend"] == "cpu")
    assert cpu["active"] is None
    assert cpu["queued"][0]["id"] == job["id"]

    resumed = client.post("/api/queue/workers/resume-all")
    assert resumed.status_code == 200
    assert all(not lane["paused"] for lane in resumed.json()["lanes"].values())

    queue.tick(0)
    cpu = next(lane for lane in client.get("/api/queue").json()["lanes"] if lane["backend"] == "cpu")
    assert cpu["active"]["id"] == job["id"]


def test_queue_page_exposes_worker_toggle_controls_and_versioned_script(client):
    page = client.get("/queue")
    assert page.status_code == 200
    assert 'data-worker-action="toggle-pause-all"' in page.text
    assert 'data-worker-action="toggle-pause"' in page.text
    assert 'data-worker-action="stop-active"' in page.text
    assert "app.js?v=4" in page.text


def test_stop_all_pauses_workers_and_skips_active_fake_jobs(client, queue, movie_payload, show_payload):
    add(client, movie_payload)
    add(client, show_payload)
    queue.tick(0)
    response = client.post("/api/queue/workers/stop-all")
    assert response.status_code == 200
    controls = response.json()["lanes"]
    assert controls["cpu"]["paused"] is True
    assert controls["qsv"]["paused"] is True
    state = client.get("/api/queue").json()
    assert all(lane["active"] is None for lane in state["lanes"])
    assert sum(job["status"] == "skipped" for job in state["history"]) == 2


def test_quiet_hours_cross_midnight_and_apply_progress_cutoff(queue, movie_payload):
    controls = queue.controls
    controls.save(WorkerSettings(
        timezone="Europe/Lisbon",
        cpu=WorkerLaneSettings(quiet_hours_enabled=True, quiet_start="18:00",
            quiet_end="01:00", quiet_cutoff_percent=50),
        qsv=WorkerLaneSettings(),
    ))
    # 20:00 UTC is 21:00 in Lisbon on this date; 02:00 UTC is 03:00 local.
    assert controls.quiet_active("cpu", datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc))
    assert not controls.quiet_active("cpu", datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))

    job = queue.enqueue(EnqueueRequest.model_validate(movie_payload))["added"][0]
    saved = queue._find(job["id"])
    assert controls.quiet_action("cpu", saved) == "finish"  # Unknown progress is conservative.
    saved.progress_known = True
    saved.progress = 49.9
    assert controls.quiet_action("cpu", saved) == "stop"
    saved.progress = 50
    assert controls.quiet_action("cpu", saved) == "finish"
