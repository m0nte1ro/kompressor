import json

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.main import create_app
from app.models.media import AudioTrack
from app.repositories.database import Database
from app.repositories.preset_seed import SeedPresetRepository
from app.repositories.sqlite import SQLitePresetRepository
from app.services.estimation import audio_plan, estimate


DEFAULT_IDS = {
    "movie-preserve-quality",
    "movie-streaming-quality",
    "show-preserve-quality",
    "show-streaming-quality",
    "show-streaming-efficient-audio",
}


@pytest.fixture
def streaming(tmp_path):
    app = create_app(tmp_path / "streaming.sqlite3", start_workers=False)
    with TestClient(app) as client:
        yield app, client


def payload(client, preset_id):
    preset = next(p for p in client.get("/api/presets").json() if p["id"] == preset_id)
    preset.pop("id")
    return preset


def test_default_catalogue_has_exactly_five_enabled_built_ins(streaming):
    _, client = streaming
    presets = client.get("/api/presets").json()
    enabled = [p for p in presets if p["origin"] == "built_in" and p["enabled"]]
    assert {p["id"] for p in enabled} == DEFAULT_IDS
    assert {p["name"] for p in enabled} == {
        "Movie Preserve Quality",
        "Movie Streaming Quality",
        "Show Preserve Quality",
        "Show Streaming Quality",
        "Show Streaming + Efficient Audio",
    }
    assert len([p for p in enabled if p["scope"] == "movie"]) == 2
    assert len([p for p in enabled if p["scope"] == "show"]) == 3
    assert {p["id"] for p in client.get("/api/presets?scope=movie").json()} == {
        "movie-preserve-quality", "movie-streaming-quality",
    }
    assert {p["id"] for p in client.get("/api/presets?scope=show").json()} == {
        "show-preserve-quality", "show-streaming-quality", "show-streaming-efficient-audio",
    }
    assert all(p["resolution_policy"] == "preserve" for p in enabled)
    assert all(p["source_resolutions"] == ["480p", "720p", "1080p", "2160p"] for p in enabled)


def test_default_backends_and_shared_show_video_policy(streaming):
    _, client = streaming
    presets = {p["id"]: p for p in client.get("/api/presets").json()}
    assert presets["movie-preserve-quality"]["backend"] == "cpu"
    assert presets["movie-streaming-quality"]["backend"] == "cpu"
    assert presets["show-preserve-quality"]["backend"] == "cpu"
    assert presets["show-streaming-quality"]["backend"] == "qsv"
    assert presets["show-streaming-efficient-audio"]["backend"] == "qsv"

    video_fields = [
        "intent", "backend", "destination_codec", "rate_control", "quality_value",
        "encoder_preset", "output_bit_depth", "source_resolutions", "hdr_support",
        "planning_video_bitrate_low", "planning_video_bitrate_high", "resolution_policy",
        "preserve_hdr_metadata", "minimum_source_bitrate", "minimum_expected_saving_percent",
        "allow_hevc_reencode",
    ]
    streaming_video = presets["show-streaming-quality"]
    efficient_video = presets["show-streaming-efficient-audio"]
    assert {field: streaming_video[field] for field in video_fields} == {
        field: efficient_video[field] for field in video_fields
    }
    assert streaming_video["audio_policy"] != efficient_video["audio_policy"]
    assert streaming_video["audio_conversion_policy"] == efficient_video["audio_conversion_policy"] == "efficient"


def test_king_of_comedy_is_eligible_for_movie_streaming(streaming):
    app, client = streaming
    app.state.catalog.find("movie-king-of-comedy", "movie").item.source = "BluRay REMUX"
    result = client.post("/api/eligibility", json={
        "scope": "movie", "media_id": "movie-king-of-comedy", "preset_id": "movie-streaming-quality",
    }).json()
    assert result["eligible"] is True
    assert not any("2160p REMUX" in reason for reason in result["reasons"])


@pytest.mark.parametrize("scope,media_id,preset_id,reason", [
    ("movie", "movie-dune-part-two", "movie-streaming-quality", "2160p REMUX"),
    ("movie", "movie-hardlinked-example", "movie-streaming-quality", "hardlinked"),
    ("show", "top-gear-s14e01", "show-streaming-quality", "Interlaced"),
])
def test_safety_guards_remain_in_force(streaming, scope, media_id, preset_id, reason):
    _, client = streaming
    result = client.post("/api/eligibility", json={
        "scope": scope, "media_id": media_id, "preset_id": preset_id,
    }).json()
    assert result["eligible"] is False
    assert any(reason in item for item in result["reasons"])


def test_dolby_vision_remains_blocked(streaming):
    _, client = streaming
    result = client.post("/api/eligibility", json={
        "scope": "movie", "media_id": "movie-dune-part-two", "preset_id": "movie-preserve-quality",
    }).json()
    assert not result["eligible"]
    assert any("Dolby Vision" in reason for reason in result["reasons"])


def test_scope_mismatch_is_blocked_by_backend(streaming):
    _, client = streaming
    for scope, media_id, preset_id in [
        ("movie", "movie-king-of-comedy", "show-streaming-quality"),
        ("show", "modern-family-s03e04", "movie-streaming-quality"),
    ]:
        result = client.post("/api/eligibility", json={
            "scope": scope, "media_id": media_id, "preset_id": preset_id,
        }).json()
        assert not result["eligible"]
        assert any("scope" in reason for reason in result["reasons"])


def test_preserve_audio_default_and_tag_override(streaming):
    app, client = streaming
    request = {"scope": "movie", "media_id": "movie-king-of-comedy", "preset_id": "movie-streaming-quality"}
    default = client.post("/api/eligibility", json=request).json()
    overridden = client.post("/api/eligibility", json={**request, "preserve_audio": False}).json()
    assert default["preserve_audio"] is True
    assert overridden["preserve_audio"] is False
    assert overridden["audio_plan"][0]["action"] == "encode"
    assert overridden["audio_plan"][0]["codec"] == "aac"

    assert client.patch("/api/tags", json={
        "targets": [{"kind": "movie", "id": "movie-king-of-comedy"}],
        "tags": ["Preserve Audio"],
    }).status_code == 200
    tagged = client.post("/api/eligibility", json={**request, "preserve_audio": False}).json()
    assert tagged["preserve_audio"] is True
    assert any("overrides" in warning for warning in tagged["warnings"])


def test_efficient_audio_rules_retain_tracks_and_channels(streaming):
    app, _ = streaming
    item = app.state.catalog.find("movie-king-of-comedy", "movie").item
    item.audio = [AudioTrack(codec=codec, channels=channels, bitrate=bitrate) for codec, channels, bitrate in [
        ("dts", 2, 1500000), ("truehd", 6, 3000000), ("aac", 2, 128000),
        ("truehd", 8, 4000000), ("ac3", 6, None),
    ]]
    preset = app.state.catalog.preset("show-streaming-efficient-audio")
    plan = audio_plan(item, preset, False)
    assert [p["action"] for p in plan] == ["encode", "encode", "copy", "encode", "copy"]
    assert [p["channels"] for p in plan] == [2, 6, 2, 8, 6]
    assert plan[0]["codec"] == "aac" and plan[0]["bitrate"] == 192000
    assert plan[1]["codec"] == "eac3" and plan[1]["bitrate"] == 640000
    assert plan[3]["codec"] == "eac3" and plan[3]["channels"] == 8
    assert all(p["action"] == "copy" for p in audio_plan(item, preset, True))


def test_duplicate_is_independent_custom_and_deletable(streaming):
    _, client = streaming
    original = client.get("/api/presets").json()
    source = next(p for p in original if p["id"] == "show-streaming-quality")
    duplicate = client.post(f"/api/presets/{source['id']}/duplicate").json()
    assert duplicate["id"] != source["id"]
    assert duplicate["origin"] == "custom"
    assert duplicate["name"] == "Show Streaming Quality (Copy)"
    assert duplicate["backend"] == source["backend"]
    assert duplicate["rate_control"] == source["rate_control"]

    edited = {key: value for key, value in duplicate.items() if key != "id"}
    edited.update(name="The Office Space Saver", resolution_policy="max_1080p")
    response = client.put(f"/api/presets/{duplicate['id']}", json=edited)
    assert response.status_code == 200
    changed = response.json()
    assert changed["resolution_policy"] == "max_1080p"
    original_after = next(p for p in client.get("/api/presets").json() if p["id"] == source["id"])
    assert original_after["resolution_policy"] == "preserve"
    assert original_after["name"] == source["name"]
    assert client.delete(f"/api/presets/{duplicate['id']}").status_code == 204
    assert client.get(f"/api/presets/{duplicate['id']}").status_code == 405
    assert client.delete("/api/presets/show-streaming-quality").status_code == 409


def test_quality_modes_return_planning_ranges_not_exact_predictions(streaming):
    _, client = streaming
    result = client.post("/api/eligibility", json={
        "scope": "movie", "media_id": "movie-king-of-comedy", "preset_id": "movie-streaming-quality",
    }).json()
    assert result["estimate_basis"] == "planning_range"
    assert result["estimated_output_size"] is None
    assert result["estimated_saving"] is None
    assert result["estimated_saving_percent"] is None
    assert result["estimated_output_size_low"] < result["estimated_output_size_high"]
    assert result["estimated_saving_low"] < result["estimated_saving_high"]
    assert result["planning_output_size"] is not None
    assert result["planning_saving"] is not None
    assert any("Planning range only" in warning for warning in result["warnings"])


@pytest.mark.parametrize("changes", [
    {"backend": "qsv"}, {"quality_value": None}, {"target_video_bitrate": 4000000},
    {"planning_video_bitrate_low": 9000000}, {"planning_video_bitrate_high": None},
    {"source_resolutions": []}, {"output_bit_depth": 12},
    {"hdr_support": "hdr10_experimental", "output_bit_depth": 8},
    {"hdr_support": "hdr10_experimental", "preserve_hdr_metadata": False},
])
def test_invalid_quality_presets_rejected(streaming, changes):
    _, client = streaming
    response = client.put("/api/presets/movie-streaming-quality", json={
        **payload(client, "movie-streaming-quality"), **changes,
    })
    assert response.status_code == 422


def test_qsv_quality_validation_and_warning(streaming):
    _, client = streaming
    source = payload(client, "show-streaming-quality")
    for quality in (0, 23.5, 52):
        assert client.post("/api/presets", json={**source, "quality_value": quality}).status_code == 422
    result = client.post("/api/eligibility", json={
        "scope": "show", "media_id": "modern-family-s03e04", "preset_id": "show-streaming-quality",
    }).json()
    assert result["eligible"]
    assert any("hardware" in warning for warning in result["warnings"])


@pytest.mark.parametrize("replace", [False, True])
def test_output_options_are_separate_from_presets(tmp_path, replace):
    path = tmp_path / "state.sqlite3"
    with TestClient(create_app(path, start_workers=False)) as client:
        before = client.get("/api/library").json()
        request = {"scope": "movie", "media_ids": ["movie-king-of-comedy"],
                   "preset_id": "movie-streaming-quality", "replace_source": replace}
        job = client.post("/api/queue", json=request).json()["added"][0]
        assert job["replace_source"] is replace
        assert job["preset"]["resolution_policy"] == "preserve"
        assert job["estimated_saving"] is None
        assert job["planning_saving"] is not None
    app = create_app(path, start_workers=False)
    with TestClient(app) as client:
        app.state.queue_service.tick(0)
        app.state.queue_service.tick(185)
        history = client.get("/api/queue").json()["history"]
        assert history[0]["replace_source"] is replace
        assert history[0]["status"] == "completed"
        assert client.get("/api/library").json() == before


def test_existing_database_upgrade_preserves_edits_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    legacy = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets-legacy.json").get_all()
    repository = SQLitePresetRepository(database, legacy)
    edited = legacy[0].model_copy(update={"name": "My edited movie preset", "target_video_bitrate": 8000000})
    repository.save(edited)
    with TestClient(create_app(path, start_workers=False)) as client:
        all_presets = client.get("/api/presets").json()
        enabled_built_ins = [p for p in all_presets if p["origin"] == "built_in" and p["enabled"]]
        assert {p["id"] for p in enabled_built_ins} == DEFAULT_IDS
        assert len(all_presets) == 6
        stored = next(p for p in all_presets if p["id"] == edited.id)
        assert stored["name"] == edited.name and stored["target_video_bitrate"] == 8000000
        assert not any(p["id"] == "show-1080p" for p in all_presets)
    with TestClient(create_app(path, start_workers=False)) as client:
        assert len(client.get("/api/presets").json()) == 6


def test_streaming_v1_catalogue_is_disabled_without_deleting_rows(tmp_path):
    path = tmp_path / "streaming-v1.sqlite3"
    database = Database(path)
    old = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets-streaming-v1.json").get_all()
    SQLitePresetRepository(database, old)
    with TestClient(create_app(path, start_workers=False)) as client:
        presets = client.get("/api/presets").json()
        assert len(presets) == 5
        assert {p["id"] for p in presets if p["origin"] == "built_in" and p["enabled"]} == DEFAULT_IDS
        assert not {p["id"] for p in presets} & {old_p.id for old_p in old}


def test_legacy_audio_rule_field_loads_from_sqlite(tmp_path):
    path = tmp_path / "legacy-audio-rules.sqlite3"
    database = Database(path)
    current = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()[0]
    legacy_payload = current.model_dump()
    rules = legacy_payload["efficient_audio_rules"]
    rules["preserve_channels"] = True
    rules.pop("channel_handling")
    with database.transaction() as connection:
        connection.execute("INSERT INTO presets VALUES (?, ?)", (current.id, json.dumps(legacy_payload)))
        connection.execute("INSERT INTO metadata VALUES ('presets_initialized', 'true')")
    with TestClient(create_app(path, start_workers=False)) as client:
        response = client.get("/settings")
        assert response.status_code == 200
        stored = next(p for p in client.get("/api/presets").json() if p["id"] == current.id)
        assert stored["efficient_audio_rules"]["channel_handling"] == "preserve"
        assert "preserve_channels" not in stored["efficient_audio_rules"]


def test_estimates_are_not_clamped_to_source_size(streaming):
    app, _ = streaming
    item = app.state.catalog.find("movie-king-of-comedy", "movie").item
    preset = app.state.catalog.preset("movie-streaming-quality").model_copy(update={
        "planning_video_bitrate_low": 30000000, "planning_video_bitrate_high": 40000000,
    })
    result = estimate(item, preset, True)
    assert result["estimated_output_size"] is None
    assert result["estimated_output_size_low"] > item.size
    assert result["planning_saving"] == 0
    eligibility = app.state.catalog.policy.evaluate(item=item, scope="movie", preset=preset,
        effective_tags=[], preserve_audio=True, preserve_subtitles=True)
    assert eligibility.eligible
    assert any("minimum saving" in warning for warning in eligibility.warnings)