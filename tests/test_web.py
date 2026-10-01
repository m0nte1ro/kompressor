import json
import re
import pytest

from app.config import PROJECT_ROOT
from app.routers.web import size


@pytest.mark.parametrize("url,title", [
    ("/movies", "Movies"), ("/shows", "Shows"),
    ("/shows/show-modern-family", "Modern Family"),
    ("/shows/show-house-of-the-dragon", "House of the Dragon"),
    ("/queue", "Queue"), ("/history", "History"), ("/settings", "Settings"),
])
def test_pages_render_and_assets_are_served(client, url, title):
    response = client.get(url)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert f"{title} · Kompressor" in response.text
    assert 'href="/movies"' in response.text


@pytest.mark.parametrize("asset,content_type", [("app.css", "text/css"), ("app.js", "javascript"), ("compression.js", "javascript"), ("queue.js", "javascript"), ("common.js", "javascript"), ("presets.js", "javascript"), ("settings.js", "javascript"), ("tags.js", "javascript")])
def test_static_assets(client, asset, content_type):
    response = client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_single_media_detail_endpoint_returns_only_requested_item(client):
    response = client.get("/api/media/show/modern-family-s03e04")
    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["id"] == "modern-family-s03e04"
    assert "name" in payload
    assert "movies" not in payload and "shows" not in payload


def test_compression_modal_does_not_fetch_full_library(client):
    script = client.get("/static/compression.js")
    assert script.status_code == 200
    assert "api('/api/library')" not in script.text
    assert "/api/media/" in script.text
    assert "loadPresets().catch" in script.text


def test_blocking_reasons_are_visible(client):
    html = client.get("/movies").text
    assert "Dune: Part Two" in html
    assert "2160p REMUX movies are automatically protected" in html
    assert "File is hardlinked" in html
    assert 'id="add-to-queue" disabled' in html
    assert 'data-preset="show-' not in html


def test_flat_episodes_and_escaped_seed_names(client, catalog):
    season = catalog.media.library.shows[0].seasons[0]
    second_season = season.model_copy(deep=True)
    second_season.season = 4
    second_season.episodes = [second_season.episodes[0]]
    second_season.episodes[0].season = 4
    second_season.episodes[0].id = "s04e04"
    season.episodes[0].title = '<script>alert("seed")</script>'
    catalog.media.library.shows[0].seasons.append(second_season)
    html = client.get("/shows/show-modern-family").text
    assert "Season 3" in html and "Season 4" in html
    assert html.count('class="media-row"') == 3
    assert 'data-preset="movie-' not in html
    assert '<script>alert("seed")</script>' not in html
    assert "&lt;script&gt;" in html
    assert 'href="/shows/show-modern-family/season' not in html


def test_settings_groups_presets_and_shows_policy_fields(client):
    html = client.get("/settings").text
    movie_start, show_start = html.index('<h2>Movie presets'), html.index('<h2>Show presets')
    assert "Just convert to HEVC" in html[movie_start:show_start]
    assert "Tone it down a bit + HEVC" in html[movie_start:show_start]
    assert "Tone it down a bit + HEVC + Efficient Audio" in html[show_start:]
    assert html[movie_start:show_start].count("Tone it down a bit + HEVC") == 1
    assert html[show_start:].count("Tone it down a bit + HEVC") >= 2
    assert "Source applicability" not in html
    assert "Planning estimate only" not in html
    assert html.count('<details class="preset-section"') == 2
    assert '<details id="audio-conversion-options" class="notice" hidden>' in html
    assert 'id="library-paths-form"' in html
    gpu_start = html.index('data-preset-id="show-streaming-quality"')
    gpu_card = html[gpu_start:html.index('</article>', gpu_start)]
    assert "Encoder effort" not in gpu_card
    assert "Validation" in gpu_card and "Experimental" in gpu_card
    assert "Output bit depth" in gpu_card
    assert "Conversion profile" in html
    assert 'class="quality-range"' in html
    assert 'id="quality-value-output"' in html
    for field in ["Intent", "Origin", "Minimum source bitrate", "Minimum expected saving", "HEVC reencode allowed", "HDR signalling"]:
        assert field in html


def test_not_found_and_home(client):
    assert client.get("/shows/missing").status_code == 404
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/movies"


def test_size_uses_binary_units():
    assert size(81_093_483_589) == "75.5 GiB"


def test_blocked_media_saves_nothing(client):
    html = client.get("/movies").text
    for media_id in ("movie-hardlinked-example", "movie-dune-part-two"):
        start = html.index(f'data-id="{media_id}"')
        row = html[start:html.index("</tr>", start)]
        assert "Blocked" in row and 'data-sort-saving="0"' in row
        assert '<td class="saving numeric"><span class="muted">0.0 GiB</span><small>Blocked</small></td>' in row


def test_eligible_rows_show_one_planning_estimate_not_a_range(client):
    html = client.get("/movies").text
    cells = re.findall(r'<td class="saving numeric">(.*?)</td>', html)
    eligible = [cell for cell in cells if "Blocked" not in cell]
    assert eligible and all(cell.startswith("~") and "–" not in cell for cell in eligible)


def test_summary_counts_only_eligible_media(client, catalog):
    html = client.get("/movies").text
    expected = sum((row["eligibility"].planning_saving or row["eligibility"].estimated_saving or 0)
                   for row in catalog.previews("movie") if row["eligibility"].eligible)
    assert "Eligible items · planning midpoint · not measured" in html
    assert f"~{size(expected)}" in html


def test_rescan_button_is_available_on_filesystem_library_pages(client, monkeypatch):
    processor = client.app.state.media_processor
    monkeypatch.setattr(processor, "get_scan_status",
                        lambda: {"backend": "filesystem", "state": "idle", "roots": []})
    for url in ["/movies", "/shows", "/shows/show-modern-family", "/settings"]:
        html = client.get(url).text
        assert 'data-scan-library' in html
        assert "Rescan library" in html


def test_show_seasons_have_select_all_controls(client, catalog):
    season = catalog.media.library.shows[0].seasons[0]
    second_season = season.model_copy(deep=True)
    second_season.season = 4
    second_season.episodes = [second_season.episodes[0]]
    second_season.episodes[0].season = 4
    second_season.episodes[0].id = "s04e04-select"
    catalog.media.library.shows[0].seasons.append(second_season)

    html = client.get("/shows/show-modern-family").text
    assert 'class="season-select" data-season="3"' in html
    assert 'class="season-select" data-season="4"' in html
    assert 'aria-label="Select all episodes in season 3"' in html


def test_discovery_script_uses_multi_button_selector_and_declares_manual_scan_state(client):
    script = client.get("/static/discovery.js").text
    assert "const buttons = $$('[data-scan-library]');" in script
    assert "let manualScanRequested = false;" in script


def test_app_script_uses_multi_season_selector(client):
    script = client.get("/static/app.js").text
    assert "$$('.season-select', library).forEach" in script
    assert not re.search(r"(?<!\$)\$\('\.season-select', library\)\.forEach", script)


def test_every_module_gets_a_content_versioned_url(client):
    page = client.get("/movies").text
    match = re.search(r'<script type="importmap">(.*?)</script>', page, re.S)
    assert match is not None
    imports = json.loads(match.group(1))["imports"]
    static = PROJECT_ROOT / "app" / "static"
    for script in static.glob("*.js"):
        assert re.fullmatch(rf"/static/{script.name}\?v=[0-9a-f]{{12}}", imports[f"/static/{script.name}"])
        # Version suffixes in imports would bypass the import map and split the module graph.
        assert not re.search(r"from '[^']+\?v=|import '[^']+\?v=", script.read_text())
    response = client.get("/static/common.js")
    assert response.headers["cache-control"] == "no-cache"


def test_history_marks_grown_outputs_red_and_offers_delete(client):
    script = client.get("/static/queue.js").text
    assert "'saving larger'" in script and 'data-history-action="delete"' in script
    assert ".saving.larger { color: var(--red); }" in client.get("/static/app.css").text
    assert "/api/queue/history/" in client.get("/static/app.js").text
