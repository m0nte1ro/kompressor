import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.queue import EnqueueRequest
from app.repositories.database import Database


def test_restart_preserves_preferences_queue_order_and_history(tmp_path):
    path = tmp_path / "state.sqlite3"
    app = create_app(path, start_workers=False)
    with TestClient(app) as client:
        preset = client.get("/api/presets?scope=movie").json()[0]
        id = preset.pop("id")
        preset.update(name="My movie quality", target_video_bitrate=9_000_000)
        client.put(f"/api/presets/{id}", json=preset)
        custom = dict(preset, name="Created preset survives restart")
        created_id = client.post("/api/presets", json=custom).json()["id"]
        disabled = client.get("/api/presets?scope=movie").json()[1]
        disabled_id = disabled.pop("id")
        client.put(f"/api/presets/{disabled_id}", json={**disabled, "enabled": False})
        client.patch("/api/tags", json={"targets": [{"kind": "movie", "id": "movie-dune-part-two"}], "tags": []})
        client.patch("/api/tags", json={"targets": [{"kind": "movie", "id": "movie-king-of-comedy"}], "tags": ["Preserve Audio"]})
        job = client.post("/api/queue", json={"scope": "movie", "preset_id": id, "media_ids": ["movie-king-of-comedy"]}).json()["added"][0]
        client.patch(f"/api/queue/{job['id']}/priority", json={"priority": "urgent"})
        client.post(f"/api/queue/{job['id']}/move-next")
        # Produce history on the other lane, then queue another job there.
        show_payload = {"scope": "show", "preset_id": "show-1080p", "media_ids": ["modern-family-s03e04"]}
        client.post("/api/queue", json=show_payload)
        app.state.queue_service.tick(0)
        app.state.queue_service.tick(30)
        active_qsv = app.state.queue_service.snapshot()["lanes"][1]["active"]
        client.post(f"/api/queue/{active_qsv['id']}/skip")
        client.post("/api/queue", json=show_payload)
    second = create_app(path, start_workers=False)
    with TestClient(second) as client:
        presets = client.get("/api/presets").json()
        assert len(presets) == 7
        assert next(p for p in presets if p["id"] == created_id)["name"] == custom["name"]
        assert next(p for p in presets if p["id"] == disabled_id)["enabled"] is False
        dune = client.get("/api/tags", params={"kind": "movie", "id": "movie-dune-part-two"}).json()
        assert dune["direct"]["tags"] == []
        assert presets[0]["name"] == "My movie quality"
        assert presets[0]["target_video_bitrate"] == 9_000_000
        tags = client.get("/api/tags", params={"kind": "movie", "id": "movie-king-of-comedy"}).json()
        assert tags["direct"]["tags"] == ["Preserve Audio"]
        state = client.get("/api/queue").json()
        assert state["history"][0]["status"] == "skipped"
        recovered = state["lanes"][0]["queued"][0]
        assert recovered["id"] == job["id"]
        assert recovered["priority"] == "urgent" and recovered["move_next_order"] > 0
        assert recovered["progress"] == 0 and recovered["elapsed_seconds"] == 0
        assert recovered["started_at"] is None
        second.state.queue_service.tick(0)
        second.state.queue_service.tick(185)
        assert all(lane["active"] is None for lane in second.state.queue_service.snapshot()["lanes"])
    with TestClient(create_app(path, start_workers=False)) as client:
        history = client.get("/api/queue").json()["history"]
        assert sum(j["status"] == "completed" for j in history) == 2


def test_queue_transaction_rolls_back_partial_batch(client, queue, catalog, monkeypatch):
    original = catalog.media.library.movies[1]
    catalog.media.library.movies.append(original.model_copy(update={"id": "another"}))
    save = queue.repository.add
    def fail_second(job):
        if job.media_id == "another":
            raise RuntimeError("Simulated storage failure")
        save(job)
    monkeypatch.setattr(queue.repository, "add", fail_second)
    with pytest.raises(RuntimeError, match="storage failure"):
        queue.enqueue(EnqueueRequest(scope="movie", preset_id="movie-1080p-quality", media_ids=[original.id, "another"]))
    assert queue.snapshot()["pending_count"] == 0


def test_database_transaction_rolls_back_and_reopens(tmp_path):
    path = tmp_path / "state.sqlite3"
    database = Database(path)
    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            connection.execute("INSERT INTO metadata VALUES ('test', 'value')")
            with database.transaction():
                raise RuntimeError("Rollback")
    with Database(path).transaction() as connection:
        assert connection.execute("SELECT 1 FROM metadata WHERE id = 'test'").fetchone() is None
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_idle_worker_tick_without_jobs(queue):
    queue.tick(1)
    assert queue.snapshot()["pending_count"] == 0


def test_tag_and_queue_revalidation_rollback_together(client, queue, show_payload, monkeypatch):
    client.post("/api/queue", json=show_payload)
    def fail_save(job):
        raise RuntimeError("Simulated storage failure")
    monkeypatch.setattr(queue.repository, "save", fail_save)
    with pytest.raises(RuntimeError, match="storage failure"):
        client.patch("/api/tags", json={"targets": [{"kind": "show", "id": "show-modern-family"}], "tags": ["Preserve A/V"]})
    detail = client.get("/api/tags", params={"kind": "show", "id": "show-modern-family"}).json()
    assert detail["direct"]["tags"] == []
    assert queue.snapshot()["lanes"][1]["queued"][0]["status"] == "queued"
