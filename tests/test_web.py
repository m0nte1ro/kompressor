import pytest


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


@pytest.mark.parametrize("asset,content_type", [("app.css", "text/css"), ("app.js", "javascript"), ("compression.js", "javascript"), ("queue.js", "javascript"), ("common.js", "javascript"), ("presets.js", "javascript"), ("tags.js", "javascript")])
def test_static_assets(client, asset, content_type):
    response = client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


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
    assert '<details id="audio-conversion-options" class="notice" hidden>' in html
    qsv_start = html.index('data-preset-id="show-streaming-quality"')
    qsv_card = html[qsv_start:html.index('</article>', qsv_start)]
    assert "Encoder effort" not in qsv_card
    assert "Validation" in qsv_card and "Experimental" in qsv_card
    assert "Output bit depth" in qsv_card
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
