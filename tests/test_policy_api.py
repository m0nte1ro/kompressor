import pytest


@pytest.mark.parametrize("scope,count", [("movie", 2), ("show", 3)])
def test_scoped_presets(client, scope, count):
    response = client.get(f"/api/presets?scope={scope}")
    assert response.status_code == 200
    assert len(response.json()) == count
    assert all(preset["scope"] == scope for preset in response.json())


@pytest.mark.parametrize("scope,media,preset,eligible,reason", [
    ("movie", "movie-dune-part-two", "movie-streaming-quality", False, "2160p REMUX"),
    ("movie", "movie-hardlinked-example", "movie-streaming-quality", False, "hardlinked"),
    ("movie", "movie-king-of-comedy", "show-streaming-quality", False, "scope"),
    ("show", "modern-family-s03e04", "movie-streaming-quality", False, "scope"),
    ("show", "modern-family-s03e04", "show-streaming-quality", True, ""),
    ("show", "modern-family-s03e05", "show-streaming-quality", False, "HEVC"),
    ("show", "top-gear-s14e01", "show-streaming-quality", False, "Interlaced"),
])
def test_eligibility_regressions(client, scope, media, preset, eligible, reason):
    response = client.post("/api/eligibility", json={"media_id": media, "scope": scope, "preset_id": preset})
    assert response.status_code == 200
    result = response.json()
    assert result["eligible"] is eligible
    if reason:
        assert any(reason in item for item in result["reasons"])
    else:
        assert result["backend"] == "qsv"
        assert result["planning_saving"] > 0


def test_dune_keeps_all_core_protections(client):
    result = client.post("/api/eligibility", json={"scope": "movie", "media_id": "movie-dune-part-two", "preset_id": "movie-preserve-quality"}).json()
    assert not result["eligible"]
    assert any("Preserve A/V" in reason for reason in result["reasons"])
    assert any("2160p REMUX" in reason for reason in result["reasons"])
    assert any("Dolby Vision" in reason for reason in result["reasons"])


def test_audio_inheritance_and_override(client, catalog):
    show = catalog.media.library.shows[0]
    show.tags.append("Preserve Audio")
    show.seasons[0].tags.append("Preserve Audio")
    request = {"scope": "show", "media_id": "modern-family-s03e04", "preset_id": "show-streaming-quality", "preserve_audio": False}
    result = client.post("/api/eligibility", json=request).json()
    assert result["preserve_audio"] is True
    assert any("tag overrides" in warning for warning in result["warnings"])
    show.seasons[0].tags.append("Preserve Video")
    result = client.post("/api/eligibility", json=request).json()
    assert result["eligible"] is False
    assert any("Preserve Video" in reason for reason in result["reasons"])


def test_api_errors_and_inventory(client):
    assert client.get("/healthz").json()["status"] == "ok"
    summary = client.get("/api/summary").json()
    assert (summary["movies"], summary["shows"], summary["episodes"]) == (3, 3, 4)
    assert len(client.get("/api/library").json()["movies"]) == 3
    assert client.get("/api/presets?scope=episode").status_code == 422
    for media, preset in [("missing", "show-streaming-quality"), ("modern-family-s03e04", "missing")]:
        assert client.post("/api/eligibility", json={"media_id": media, "scope": "show", "preset_id": preset}).status_code == 404
