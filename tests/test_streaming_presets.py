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
        "Just convert to HEVC",
        "Tone it down a bit + HEVC",
        "Tone it down a bit + HEVC + Efficient Audio",
    }
    assert len([p for p in enabled if p["scope"] == "movie"]) == 2
    assert len([p for p in enabled if p["scope"] == "show"]) == 3
    assert {p["id"] for p in client.get("/api/presets?scope=movie").json()} == {
        "movie-preserve-quality", "movie-streaming-quality",
    }
    assert {p["id"] for p in client.get("/api/presets?scope=show").json()} == {
        "show-preserve-quality", "show-streaming-quality", "show-streaming-efficient-audio",
    }
    assert all(p["target_resolution"] == "keep" for p in enabled)
    assert all(p["source_resolutions"] == ["480p", "576p", "720p", "1080p", "2160p"] for p in enabled)
    assert all(p["hdr_support"] == "hdr10_experimental" and p["hdr_policy"] == "preserve_source" for p in enabled)
    assert all(set(p["hdr_metadata"]) >= {
        "validate_signalling", "preserve_color_primaries", "preserve_transfer_characteristics",
        "preserve_matrix_coefficients", "preserve_mastering_display_metadata", "preserve_max_cll", "preserve_max_fall",
    } for p in enabled)


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
        "planning_video_bitrate_low", "planning_video_bitrate_high", "target_resolution",
        "preserve_hdr_metadata", "minimum_source_bitrate", "minimum_expected_saving_percent",
        "allow_hevc_reencode",
    ]
    streaming_video = presets["show-streaming-quality"]
    efficient_video = presets["show-streaming-efficient-audio"]
    assert {field: streaming_video[field] for field in video_fields} == {
        field: efficient_video[field] for field in video_fields
    }
    assert streaming_video["audio_policy"] != efficient_video["audio_policy"]
    assert streaming_video["audio_conversion_policy"] == "preserve"
    assert efficient_video["audio_conversion_policy"] == "efficient"
    assert all(
        presets[preset_id]["audio_policy"] == "preserve"
        and presets[preset_id]["audio_conversion_policy"] == "preserve"
        and presets[preset_id]["target_audio_bitrate"] is None
        for preset_id in ("movie-preserve-quality", "movie-streaming-quality", "show-preserve-quality", "show-streaming-quality")
    )


def test_hdr10_source_preserves_hdr_mode_without_tone_mapping(streaming):
    app, _ = streaming
    item = next(
        episode
        for show in app.state.media_processor.catalog.media.get_library().shows
        for season in show.seasons
        for episode in season.episodes
        if episode.id == "hotd-s01e01"
    )
    item.video_codec = "h264"
    result = app.state.media_processor.catalog.policy.evaluate(
        item=item,
        scope="show",
        preset=app.state.media_processor.catalog.preset("show-preserve-quality"),
        effective_tags=item.tags + ["Quality CPU"],
        preserve_audio=True,
        preserve_subtitles=True,
    )
    assert result.eligible is True
    assert any("HDR10" in warning for warning in result.warnings)
    assert not any("SDR" in reason for reason in result.reasons)


def test_movie_built_ins_force_audio_preservation(streaming):
    _, client = streaming
    for preset_id in ("movie-preserve-quality", "movie-streaming-quality"):
        result = client.post("/api/eligibility", json={
            "scope": "movie", "media_id": "movie-king-of-comedy", "preset_id": preset_id,
            "preserve_audio": False,
        }).json()
        assert result["preserve_audio"] is True
        assert all(track["action"] == "copy" for track in result["audio_plan"])


@pytest.mark.parametrize("target_resolution", ["keep", "max_2160p", "max_1080p", "max_720p", "max_576p", "max_480p"])
def test_custom_target_resolution_options_are_supported(streaming, target_resolution):
    _, client = streaming
    custom = payload(client, "show-streaming-quality")
    custom.update(name=f"Custom {target_resolution}", target_resolution=target_resolution)
    response = client.post("/api/presets", json=custom)
    assert response.status_code == 201
    assert response.json()["target_resolution"] == target_resolution


def test_built_in_tone_mapping_is_rejected(streaming):
    _, client = streaming
    custom = payload(client, "movie-streaming-quality")
    custom.update(hdr_policy="tone_map_to_sdr", preserve_hdr_metadata=False,
                  hdr_metadata={key: False for key in [
                      "validate_signalling", "preserve_color_primaries", "preserve_transfer_characteristics",
                      "preserve_matrix_coefficients", "preserve_mastering_display_metadata", "preserve_max_cll", "preserve_max_fall",
                  ]})
    assert client.put("/api/presets/movie-streaming-quality", json=custom).status_code == 422


def test_tone_mapping_requires_hdr10_input_support(streaming):
    _, client = streaming
    custom = payload(client, "movie-streaming-quality")
    custom.update(name="Explicit SDR tone map", hdr_support="sdr_only", hdr_policy="tone_map_to_sdr",
                  preserve_hdr_metadata=False,
                  hdr_metadata={key: False for key in [
                      "validate_signalling", "preserve_color_primaries", "preserve_transfer_characteristics",
                      "preserve_matrix_coefficients", "preserve_mastering_display_metadata", "preserve_max_cll", "preserve_max_fall",
                  ]})
    assert client.post("/api/presets", json=custom).status_code == 422


def test_new_preset_defaults_to_all_supported_source_resolutions(streaming):
    _, client = streaming
    custom = payload(client, "show-streaming-quality")
    custom.pop("source_resolutions")
    custom["name"] = "All resolution inputs"
    response = client.post("/api/presets", json=custom)
    assert response.status_code == 201
    assert response.json()["source_resolutions"] == ["480p", "576p", "720p", "1080p", "2160p"]


def test_quality_preset_planning_defaults_follow_intent(streaming):
    _, client = streaming
    custom = payload(client, "movie-preserve-quality")
    custom.pop("planning_video_bitrate_low")
    custom.pop("planning_video_bitrate_high")
    custom["name"] = "Intent planning defaults"
    response = client.post("/api/presets", json=custom)
    assert response.status_code == 201
    assert response.json()["planning_video_bitrate_low"] == 1_000_000
    assert response.json()["planning_video_bitrate_high"] == 30_000_000


def test_king_of_comedy_is_eligible_for_movie_streaming(streaming):
    app, client = streaming
    app.state.media_processor.catalog.find("movie-king-of-comedy", "movie").item.source = "BluRay REMUX"
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
    assert overridden["preserve_audio"] is True
    assert all(track["action"] == "copy" for track in overridden["audio_plan"])

    conversion = payload(client, "movie-streaming-quality")
    conversion.update(name="Movie streaming with opt-in audio conversion",
                      audio_conversion_policy="efficient", target_audio_bitrate=640000)
    conversion_id = client.post("/api/presets", json=conversion).json()["id"]
    conversion_request = {"scope": "movie", "media_id": "movie-king-of-comedy", "preset_id": conversion_id,
                          "preserve_audio": False}
    converted = client.post("/api/eligibility", json=conversion_request).json()
    assert converted["preserve_audio"] is False
    assert converted["audio_plan"][0]["action"] == "encode"
    assert converted["audio_plan"][0]["codec"] == "aac"

    assert client.patch("/api/tags", json={
        "targets": [{"kind": "movie", "id": "movie-king-of-comedy"}],
        "tags": ["Preserve Audio"],
    }).status_code == 200
    tagged = client.post("/api/eligibility", json=conversion_request).json()
    assert tagged["preserve_audio"] is True
    assert any("overrides" in warning for warning in tagged["warnings"])


def test_efficient_audio_rules_retain_tracks_and_channels(streaming):
    app, _ = streaming
    item = app.state.media_processor.catalog.find("movie-king-of-comedy", "movie").item
    item.audio = [AudioTrack(codec=codec, channels=channels, bitrate=bitrate) for codec, channels, bitrate in [
        ("dts", 2, 1500000), ("truehd", 6, 3000000), ("aac", 2, 128000),
        ("truehd", 8, 4000000), ("ac3", 6, None),
    ]]
    preset = app.state.media_processor.catalog.preset("show-streaming-efficient-audio")
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
    assert duplicate["name"] == f"{source['name']} (Copy)"
    assert duplicate["backend"] == source["backend"]
    assert duplicate["rate_control"] == source["rate_control"]

    edited = {key: value for key, value in duplicate.items() if key != "id"}
    edited.update(name="The Office Space Saver", target_resolution="max_1080p")
    response = client.put(f"/api/presets/{duplicate['id']}", json=edited)
    assert response.status_code == 200
    changed = response.json()
    assert changed["target_resolution"] == "max_1080p"
    original_after = next(p for p in client.get("/api/presets").json() if p["id"] == source["id"])
    assert original_after["target_resolution"] == "keep"
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
    for quality in (0, 17, 23.5, 31, 52):
        assert client.post("/api/presets", json={**source, "quality_value": quality}).status_code == 422
    for quality in (18, 23, 30):
        assert client.post("/api/presets", json={**source, "name": f"ICQ {quality}", "quality_value": quality}).status_code == 201
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
        assert job["preset"]["target_resolution"] == "keep"
        assert job["estimated_saving"] is None
        assert job["planning_saving"] is not None
    app = create_app(path, start_workers=False)
    with TestClient(app) as client:
        app.state.media_processor.queue.tick(0)
        app.state.media_processor.queue.tick(185)
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


def test_legacy_resolution_policy_loads_as_target_resolution(streaming):
    app, _ = streaming
    preset = app.state.media_processor.catalog.preset("show-streaming-quality")
    legacy = preset.model_dump()
    legacy.pop("target_resolution")
    legacy["resolution_policy"] = "max_720p"
    loaded = type(preset).model_validate(legacy)
    assert loaded.target_resolution == "max_720p"


def test_current_catalogue_migration_canonicalizes_resolution_fields(tmp_path):
    path = tmp_path / "pre-v4.sqlite3"
    database = Database(path)
    current = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    with database.transaction() as connection:
        for preset in current:
            payload = preset.model_dump()
            payload["source_resolutions"] = ["480p", "720p", "1080p", "2160p"]
            payload["resolution_policy"] = "preserve"
            payload.pop("target_resolution")
            connection.execute("INSERT INTO presets VALUES (?, ?)", (preset.id, json.dumps(payload)))
        connection.executemany("INSERT INTO metadata VALUES (?, 'true')", [
            ("presets_initialized",), ("preset_catalog_v3",),
        ])
    with TestClient(create_app(path, start_workers=False)) as client:
        presets = client.get("/api/presets").json()
        assert all(p["target_resolution"] == "keep" for p in presets)
        assert all(p["source_resolutions"] == ["480p", "576p", "720p", "1080p", "2160p"] for p in presets)


def test_catalogue_migration_does_not_rewrite_builtin_names(tmp_path):
    path = tmp_path / "named-built-in.sqlite3"
    database = Database(path)
    current = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    preset = current[0].model_copy(update={"name": "My Movie Name"})
    with database.transaction() as connection:
        connection.execute("INSERT INTO presets VALUES (?, ?)", (preset.id, preset.model_dump_json()))
        connection.executemany("INSERT INTO metadata VALUES (?, 'true')", [
            ("presets_initialized",), ("preset_catalog_v7",),
        ])
    with TestClient(create_app(path, start_workers=False)) as client:
        stored = next(p for p in client.get("/api/presets").json() if p["id"] == preset.id)
        assert stored["name"] == "My Movie Name"


def test_built_in_audio_migration_restores_preserve_defaults(tmp_path):
    path = tmp_path / "pre-audio-v6.sqlite3"
    database = Database(path)
    current = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    with database.transaction() as connection:
        for preset in current:
            payload = preset.model_dump()
            if preset.id in {"movie-streaming-quality", "show-streaming-quality"}:
                payload["audio_policy"] = "efficient"
                payload["preserve_audio_by_default"] = False
                payload["audio_conversion_policy"] = "preserve"
                payload["target_audio_bitrate"] = 640000
            connection.execute("INSERT INTO presets VALUES (?, ?)", (preset.id, json.dumps(payload)))
        connection.executemany("INSERT INTO metadata VALUES (?, 'true')", [
            ("presets_initialized",), ("preset_catalog_v5",),
        ])
    with TestClient(create_app(path, start_workers=False)) as client:
        presets = {p["id"]: p for p in client.get("/api/presets").json()}
        for preset_id in ("movie-preserve-quality", "movie-streaming-quality", "show-preserve-quality", "show-streaming-quality"):
            assert presets[preset_id]["audio_policy"] == "preserve"
            assert presets[preset_id]["audio_conversion_policy"] == "preserve"
            assert presets[preset_id]["target_audio_bitrate"] is None


def test_efficient_audio_builtin_migration_restores_conversion_defaults(tmp_path):
    path = tmp_path / "pre-audio-v7.sqlite3"
    database = Database(path)
    current = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets.json").get_all()
    efficient = next(p for p in current if p.id == "show-streaming-efficient-audio")
    payload = efficient.model_dump()
    payload["audio_policy"] = "efficient"
    payload["audio_conversion_policy"] = "preserve"
    payload["preserve_audio_by_default"] = True
    with database.transaction() as connection:
        connection.execute("INSERT INTO presets VALUES (?, ?)", (efficient.id, json.dumps(payload)))
        connection.executemany("INSERT INTO metadata VALUES (?, 'true')", [
            ("presets_initialized",), ("preset_catalog_v6",),
        ])
    with TestClient(create_app(path, start_workers=False)) as client:
        stored = next(p for p in client.get("/api/presets").json() if p["id"] == efficient.id)
        assert stored["audio_policy"] == "efficient"
        assert stored["audio_conversion_policy"] == "efficient"
        assert stored["preserve_audio_by_default"] is False
        assert stored["target_audio_bitrate"] == 640000


def test_estimates_are_not_clamped_to_source_size(streaming):
    app, _ = streaming
    item = app.state.media_processor.catalog.find("movie-king-of-comedy", "movie").item
    preset = app.state.media_processor.catalog.preset("movie-streaming-quality").model_copy(update={
        "planning_video_bitrate_low": 30000000, "planning_video_bitrate_high": 40000000,
    })
    result = estimate(item, preset, True)
    assert result["estimated_output_size"] is None
    assert result["estimated_output_size_low"] > item.size
    assert result["planning_saving"] == 0
    eligibility = app.state.media_processor.catalog.policy.evaluate(item=item, scope="movie", preset=preset,
        effective_tags=[], preserve_audio=True, preserve_subtitles=True)
    assert eligibility.eligible
    assert any("minimum saving" in warning for warning in eligibility.warnings)