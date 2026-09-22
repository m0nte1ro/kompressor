import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.main import create_app
from app.models.media import AudioTrack
from app.repositories.database import Database
from app.repositories.preset_seed import SeedPresetRepository
from app.repositories.sqlite import SQLitePresetRepository
from app.services.estimation import audio_plan, estimate


@pytest.fixture
def streaming(tmp_path):
    app = create_app(tmp_path / "streaming.sqlite3", start_workers=False)
    with TestClient(app) as client:
        yield app, client


def payload(client, id="movie-streaming-1080p"):
    preset = next(p for p in client.get("/api/presets").json() if p["id"] == id)
    preset.pop("id")
    return preset


def test_default_coverage_and_quality_settings(streaming):
    _, client = streaming
    presets = client.get("/api/presets").json()
    assert len(presets) == 10
    for scope in ("movie", "show"):
        defaults = [p for p in presets if p["scope"] == scope and p["backend"] == "cpu"]
        assert {tuple(p["source_resolutions"]) for p in defaults} == {("480p",), ("720p",), ("1080p",), ("2160p",)}
        assert all(p["rate_control"] == "crf" and p["target_video_bitrate"] is None for p in defaults)
        assert all(not p["allow_hevc_reencode"] and p["output_bit_depth"] == 10 for p in defaults)
        assert all(p["resolution_policy"] == "preserve" and p["hdr_support"] == "sdr_only" for p in defaults)
    movie = payload(client)
    assert movie["quality_value"] == 22 and movie["encoder_preset"] == "slow"
    assert movie["planning_video_bitrate_low"] == 2_000_000
    assert movie["planning_video_bitrate_high"] == 6_000_000


def test_king_of_comedy_is_a_streaming_candidate_with_uncertain_estimates(streaming):
    app, client = streaming
    movie = app.state.catalog.find("movie-king-of-comedy", "movie")
    movie.item.source = "BluRay REMUX"
    result = app.state.catalog.evaluate(movie, app.state.catalog.preset("movie-streaming-1080p"))
    assert result.eligible
    assert result.estimate_basis == "planning_range"
    assert result.estimated_output_size_low < result.estimated_output_size_high
    assert result.estimated_saving_percent > 60
    assert any("outside" in warning for warning in result.warnings)
    assert 'data-preset="movie-streaming-1080p"' in client.get("/movies").text


def test_wrong_resolution_hdr_and_uhd_remux_remain_blocked(streaming):
    app, client = streaming
    entry = app.state.catalog.find("movie-king-of-comedy", "movie")
    entry.item.resolution, entry.item.height = "2160p", 2160
    preset = app.state.catalog.preset("movie-streaming-1080p")
    assert any("resolution" in r for r in app.state.catalog.evaluate(entry, preset).reasons)
    preset = app.state.catalog.preset("movie-streaming-2160p")
    entry.item.hdr = "hdr10"
    assert any("SDR" in r for r in app.state.catalog.evaluate(entry, preset).reasons)
    result = client.post("/api/eligibility", json={"scope": "movie", "media_id": "movie-dune-part-two", "preset_id": preset.id}).json()
    assert not result["eligible"]
    assert any("REMUX" in r for r in result["reasons"])
    assert any("Dolby Vision" in r for r in result["reasons"])


@pytest.mark.parametrize("changes", [
    {"backend": "qsv"}, {"quality_value": None}, {"target_video_bitrate": 4000000},
    {"planning_video_bitrate_low": 7000000}, {"planning_video_bitrate_high": None},
    {"source_resolutions": []}, {"output_bit_depth": 12},
    {"hdr_support": "hdr10_experimental", "output_bit_depth": 8},
    {"hdr_support": "hdr10_experimental", "preserve_hdr_metadata": False},
])
def test_invalid_quality_presets_rejected(streaming, changes):
    _, client = streaming
    response = client.put("/api/presets/movie-streaming-1080p", json={**payload(client), **changes})
    assert response.status_code == 422


def test_qsv_integer_quality_and_rate_control_validation(streaming):
    _, client = streaming
    preset = payload(client, "show-streaming-1080p-qsv")
    for quality in (0, 23.5, 52):
        assert client.post("/api/presets", json={**preset, "quality_value": quality}).status_code == 422
    result = client.post("/api/eligibility", json={"scope": "show", "media_id": "modern-family-s03e04", "preset_id": "show-streaming-1080p-qsv"}).json()
    assert result["eligible"]
    assert any("hardware" in warning for warning in result["warnings"])


def test_quality_floor_does_not_mistake_planning_bitrate_for_guarantee(streaming):
    _, client = streaming
    client.patch("/api/tags", json={"targets": [{"kind": "movie", "id": "movie-king-of-comedy"}],
        "tags": ["Quality Floor"], "quality_floor": {"minimum_height": 1080, "minimum_video_bitrate": 1000000}})
    result = client.post("/api/eligibility", json={"scope": "movie", "media_id": "movie-king-of-comedy", "preset_id": "movie-streaming-1080p"}).json()
    assert not result["eligible"]
    assert any("cannot be guaranteed" in r for r in result["reasons"])


def test_audio_plan_handles_every_track_without_downmix(streaming):
    app, _ = streaming
    item = app.state.catalog.find("movie-king-of-comedy", "movie").item
    item.audio = [AudioTrack(codec=codec, channels=channels, bitrate=bitrate) for codec, channels, bitrate in [
        ("dts", 2, 1500000), ("truehd", 6, 3000000), ("aac", 2, 128000),
        ("truehd", 8, 4000000), ("ac3", 6, None),
    ]]
    preset = app.state.catalog.preset("movie-streaming-1080p")
    plan = audio_plan(item, preset, False)
    assert [p["action"] for p in plan] == ["encode", "encode", "copy", "copy", "copy"]
    assert [p["channels"] for p in plan] == [2, 6, 2, 8, 6]
    assert plan[0]["codec"] == "aac" and plan[0]["bitrate"] == 192000
    assert plan[1]["codec"] == "eac3" and plan[1]["bitrate"] == 640000
    assert all(p["action"] == "copy" for p in audio_plan(item, preset, True))


@pytest.mark.parametrize("replace", [False, True])
def test_output_handling_survives_restart_without_touching_sources(tmp_path, replace):
    path = tmp_path / "state.sqlite3"
    with TestClient(create_app(path, start_workers=False)) as client:
        before = client.get("/api/library").json()
        request = {"scope": "movie", "media_ids": ["movie-king-of-comedy"], "preset_id": "movie-streaming-1080p"}
        if replace:
            request["replace_source"] = True
        job = client.post("/api/queue", json=request).json()["added"][0]
        assert job["replace_source"] is replace
        assert job["preset"]["rate_control"] == "crf"
    app = create_app(path, start_workers=False)
    with TestClient(app) as client:
        app.state.queue_service.tick(0)
        app.state.queue_service.tick(185)
        history = client.get("/api/queue").json()["history"]
        assert history[0]["replace_source"] is replace
        assert history[0]["status"] == "completed"
        assert client.get("/api/library").json() == before
        assert 'Keep original' in client.get("/movies").text


def test_estimates_are_not_clamped_to_source_size(streaming):
    app, _ = streaming
    item = app.state.catalog.find("movie-king-of-comedy", "movie").item
    preset = app.state.catalog.preset("movie-streaming-1080p").model_copy(update={
        "planning_video_bitrate_low": 30000000, "planning_video_bitrate_high": 40000000})
    result = estimate(item, preset, True)
    assert result["estimated_output_size_low"] > item.size
    assert result["estimated_saving"] == 0
    eligibility = app.state.catalog.policy.evaluate(item=item, scope="movie", preset=preset,
        effective_tags=[], preserve_audio=True, preserve_subtitles=True)
    assert eligibility.eligible  # A test copy is permitted; savings cannot be certified before encoding.
    assert any("minimum saving" in warning for warning in eligibility.warnings)


def test_existing_database_upgrade_preserves_edits_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    legacy = SeedPresetRepository(PROJECT_ROOT / "fixtures/presets-legacy.json").get_all()
    repository = SQLitePresetRepository(database, legacy)
    edited = legacy[0].model_copy(update={"name": "My edited movie preset", "target_video_bitrate": 8000000})
    repository.save(edited)
    with TestClient(create_app(path, start_workers=False)) as client:
        all_presets = client.get("/api/presets").json()
        assert len(all_presets) == 16
        stored = next(p for p in all_presets if p["id"] == edited.id)
        assert stored["name"] == edited.name and stored["target_video_bitrate"] == 8000000
        assert not next(p for p in all_presets if p["id"] == "show-1080p")["enabled"]
        custom = payload(client)
        custom["quality_value"] = 21
        assert client.put("/api/presets/movie-streaming-1080p", json=custom).status_code == 200
    with TestClient(create_app(path, start_workers=False)) as client:
        assert len(client.get("/api/presets").json()) == 16
        assert payload(client)["quality_value"] == 21
