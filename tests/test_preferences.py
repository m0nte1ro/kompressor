import pytest


def settings(client, preset_id="show-1080p"):
    preset = next(p for p in client.get("/api/presets").json() if p["id"] == preset_id)
    preset.pop("id")
    return preset


def tag(client, kind, id, tags, **extra):
    target = {"kind": kind, "id": id}
    if "season" in extra:
        target["season"] = extra.pop("season")
    return client.patch("/api/tags", json={"targets": [target], "tags": tags, **extra})


def eligible(client, media="modern-family-s03e04", preset="show-1080p", **extra):
    return client.post("/api/eligibility", json={"scope": "show", "media_id": media,
                                                "preset_id": preset, **extra}).json()


def test_create_edit_duplicate_and_disable_presets(client, show_payload):
    payload = settings(client)
    payload["name"] = "Custom show preset"
    response = client.post("/api/presets", json=payload)
    assert response.status_code == 201
    preset = response.json()
    payload.update(target_video_bitrate=3_000_000, backend="cpu", name="Edited show preset")
    assert client.put(f"/api/presets/{preset['id']}", json=payload).status_code == 200
    scoped = client.get("/api/presets?scope=movie").json()
    assert all(p["id"] != preset["id"] for p in scoped)
    duplicate = client.post(f"/api/presets/{preset['id']}/duplicate").json()
    assert duplicate["id"] != preset["id"]
    assert duplicate["target_video_bitrate"] == 3_000_000
    assert duplicate["name"] == "Edited show preset (copy)"
    payload["enabled"] = False
    client.put(f"/api/presets/{preset['id']}", json=payload)
    result = client.post("/api/queue", json={**show_payload, "preset_id": preset["id"]}).json()
    assert result["added"] == []
    assert "Preset is disabled." in result["excluded"][0]["reasons"]
    assert client.put("/api/presets/missing", json=payload).status_code == 404
    assert client.post("/api/presets/missing/duplicate").status_code == 404


@pytest.mark.parametrize("change", [
    {"name": "   "}, {"scope": "episode"}, {"target_video_bitrate": 0},
    {"target_video_bitrate": -1}, {"minimum_expected_saving_percent": 100},
    {"minimum_source_bitrate": -1}, {"target_audio_bitrate": None},
    {"destination_codec": "av1"}, {"extra_encoder_flag": "anything"},
])
def test_invalid_preset_edits_are_atomic(client, change):
    payload = settings(client)
    before = client.get("/api/presets").json()
    assert client.put("/api/presets/show-1080p", json={**payload, **change}).status_code == 422
    assert client.get("/api/presets").json() == before


def test_disabled_all_presets_keeps_library_accessible(client):
    for preset in client.get("/api/presets").json():
        id = preset.pop("id")
        preset["enabled"] = False
        assert client.put(f"/api/presets/{id}", json=preset).status_code == 200
    for page in ("/movies", "/shows/show-modern-family"):
        response = client.get(page)
        assert response.status_code == 200
        assert "No enabled preset" in response.text
    assert client.get("/settings").status_code == 200


def test_direct_and_inherited_tag_provenance(client):
    assert tag(client, "show", "show-modern-family", ["Preserve Audio"]).status_code == 200
    assert tag(client, "season", "show-modern-family", ["Quality CPU"], season=3).status_code == 200
    assert tag(client, "episode", "modern-family-s03e04", ["Preserve Video"]).status_code == 200
    detail = client.get("/api/tags", params={"kind": "episode", "id": "modern-family-s03e04"}).json()
    assert detail["direct"]["tags"] == ["Preserve Video"]
    assert detail["inherited"][0]["tags"] == ["Preserve Audio"]
    assert detail["inherited"][1]["tags"] == ["Quality CPU"]
    assert "Season 3" in detail["inherited"][1]["source"]
    assert set(detail["effective_tags"]) == {"Preserve Audio", "Quality CPU", "Preserve Video"}
    tag(client, "episode", "modern-family-s03e04", [])
    result = eligible(client, preserve_audio=False)
    assert result["preserve_audio"] is True
    assert any("Quality CPU" in reason for reason in result["reasons"])
    assert eligible(client, preset="show-quality")["eligible"] is True
    assert client.get("/api/library").json()["shows"][0]["tags"] == ["Preserve Audio"]


def test_bulk_add_remove_preserves_other_and_inherited_tags(client):
    tag(client, "show", "show-modern-family", ["Preserve Audio"])
    tag(client, "episode", "modern-family-s03e04", ["Quality CPU"])
    targets = [{"kind": "episode", "id": id} for id in ("modern-family-s03e04", "modern-family-s03e05")]
    for operation in ("add", "remove"):
        response = client.patch("/api/tags", json={"targets": targets, "tags": ["Preserve Video"], "operation": operation})
        assert response.status_code == 200
        first = response.json()["updated"][0]
        assert "Quality CPU" in first["direct"]["tags"]
        assert "Preserve Audio" in first["effective_tags"]
        assert ("Preserve Video" in first["direct"]["tags"]) == (operation == "add")
    assert client.patch("/api/tags", json={"targets": targets, "tags": []}).status_code == 422


def test_quality_floor_inheritance_and_removal(client):
    floor = {"minimum_video_bitrate": 3_000_000, "minimum_height": 720}
    assert tag(client, "show", "show-modern-family", ["Quality Floor"], quality_floor=floor).status_code == 200
    assert tag(client, "season", "show-modern-family", ["Quality Floor"], season=3,
               quality_floor={"minimum_video_bitrate": 2_000_000, "minimum_height": 1080}).status_code == 200
    result = eligible(client, preset="show-720p")
    assert not result["eligible"]
    assert any("resolution" in reason for reason in result["reasons"])
    assert any("bitrate" in reason for reason in result["reasons"])
    detail = client.get("/api/tags", params={"kind": "episode", "id": "modern-family-s03e04"}).json()
    assert detail["effective_quality_floor"] == {"minimum_video_bitrate": 3_000_000, "minimum_height": 1080}
    assert eligible(client, preset="show-quality")["eligible"] is True
    assert tag(client, "season", "show-modern-family", ["Quality Floor"], season=3, operation="remove").status_code == 200
    detail = client.get("/api/tags", params={"kind": "episode", "id": "modern-family-s03e04"}).json()
    assert detail["effective_quality_floor"] == floor


def test_invalid_tags_and_missing_target_roll_back_bulk(client):
    targets = [{"kind": "movie", "id": "movie-king-of-comedy"}, {"kind": "movie", "id": "missing"}]
    assert client.patch("/api/tags", json={"targets": targets, "tags": ["Preserve Video"], "operation": "add"}).status_code == 404
    detail = client.get("/api/tags", params=targets[0]).json()
    assert detail["direct"]["tags"] == []
    assert tag(client, "movie", "movie-king-of-comedy", ["Unknown"]).status_code == 422
    assert tag(client, "movie", "movie-king-of-comedy", ["Quality Floor"]).status_code == 422
    assert tag(client, "movie", "movie-king-of-comedy", [], quality_floor={"minimum_video_bitrate": 1, "minimum_height": 720}).status_code == 422
    assert client.get("/api/tags", params={"kind": "season", "id": "show-modern-family"}).status_code == 422
    assert client.get("/api/tags", params={"kind": "movie", "id": "missing"}).status_code == 404


def test_removing_seed_protection_does_not_override_automatic_guards(client):
    response = tag(client, "movie", "movie-dune-part-two", [])
    assert response.status_code == 200
    result = client.post("/api/eligibility", json={"scope": "movie", "media_id": "movie-dune-part-two", "preset_id": "movie-4k-quality"}).json()
    assert not result["eligible"]
    assert any("REMUX" in reason for reason in result["reasons"])
    assert any("Dolby Vision" in reason for reason in result["reasons"])


@pytest.mark.parametrize("active", [False, True])
def test_new_protection_blocks_pending_or_active_job(client, queue, show_payload, active):
    client.post("/api/queue", json=show_payload)
    if active:
        queue.tick(0)
    assert tag(client, "show", "show-modern-family", ["Preserve A/V"]).status_code == 200
    state = client.get("/api/queue").json()
    assert state["pending_count"] == 0
    assert state["history"][0]["status"] == "blocked"
    assert any("Preserve A/V" in reason for reason in state["history"][0]["reasons"])
    queue.tick(300)
    assert client.get("/api/queue").json()["history"][0]["status"] == "blocked"


def test_audio_tag_recalculates_queued_job_before_execution(client, queue, show_payload):
    client.post("/api/queue", json={**show_payload, "preserve_audio": False})
    before = queue.snapshot()["lanes"][1]["queued"][0]
    assert not before["preserve_audio"]
    tag(client, "show", "show-modern-family", ["Preserve Audio"])
    after = queue.snapshot()["lanes"][1]["queued"][0]
    assert after["preserve_audio"]
    assert after["estimated_saving"] < before["estimated_saving"]
    queue.tick(0)
    assert queue.snapshot()["lanes"][1]["active"]["preserve_audio"]


def test_preset_edits_do_not_modify_existing_jobs(client, queue, show_payload):
    original = client.post("/api/queue", json=show_payload).json()["added"][0]
    payload = settings(client)
    payload.update(enabled=False, backend="cpu", target_video_bitrate=5_000_000)
    client.put("/api/presets/show-1080p", json=payload)
    queue.tick(0)
    job = queue.snapshot()["lanes"][1]["active"]
    assert job["preset"] == original["preset"]
    assert job["backend"] == "qsv"
