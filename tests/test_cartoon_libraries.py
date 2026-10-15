"""Separate animation libraries reuse the movie/show encoding pipeline."""
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.preferences import LibraryPaths
from app.services.library_discovery import configured_roots, library_scope
from app.services.filesystem_scanner import root_identity
from app.services.errors import InvalidOperation


def test_four_roots_preserve_existing_media_scopes(tmp_path):
    paths = LibraryPaths(movies_path=str(tmp_path / "movies"),
                         shows_path=str(tmp_path / "shows"),
                         cartoons_path=str(tmp_path / "cartoons"),
                         cartoons_filmes_path=str(tmp_path / "cartoons-filmes"))
    roots = configured_roots(paths, tmp_path / "state.sqlite3")
    assert set(roots) == {"movie", "show", "cartoons", "cartoons_filmes"}
    assert [library_scope(key) for key in roots] == ["movie", "show", "show", "movie"]
    assert root_identity("show", roots["cartoons"]) != root_identity("show", roots["show"])


def test_all_pairs_of_roots_cannot_overlap(tmp_path):
    base = tmp_path / "cartoons"
    with pytest.raises(InvalidOperation, match="overlap"):
        configured_roots(LibraryPaths(
            movies_path=str(base), cartoons_path=str(base / "more")),
            tmp_path / "state.sqlite3")


def test_persisted_legacy_roots_inherit_new_environment_defaults(tmp_path):
    from app.repositories.database import Database
    from app.repositories.preferences import SQLitePreferencesRepository
    import json
    database = Database(tmp_path / "prefs.sqlite3")
    repo = SQLitePreferencesRepository(database, LibraryPaths(
        movies_path="/media/movies", shows_path="/media/shows",
        cartoons_path="/media/cartoons",
        cartoons_filmes_path="/media/cartoons-filmes"))
    with database.transaction() as connection:
        connection.execute("INSERT INTO metadata VALUES (?, ?)", (
            "library_paths", json.dumps({"movies_path": "/old/movies", "shows_path": "/old/shows"})))
    got = repo.get_library_paths()
    assert got.movies_path == "/old/movies"
    assert got.cartoons_path == "/media/cartoons"
    assert got.cartoons_filmes_path == "/media/cartoons-filmes"
    repo.save_library_paths(got.model_copy(update={"cartoons_path": ""}))
    assert repo.get_library_paths().cartoons_path == ""


def test_new_sidebar_and_pages_in_seed_mode(tmp_path):
    app = create_app(config=Settings(media_backend="seed",
        database_path=tmp_path / "seed.sqlite3"))
    with TestClient(app) as client:
        home = client.get("/movies")
        assert home.status_code == 200
        assert 'href="/cartoons"' in home.text
        assert 'href="/cartoons-filmes"' in home.text
        assert client.get("/cartoons").status_code == 200
        assert client.get("/cartoons-filmes").status_code == 200
        assert client.get("/cartoons/missing").status_code == 404


def test_four_library_sections_have_independent_inventory(tmp_path, monkeypatch):
    """Scan every mount but show each title in exactly its own section."""
    from unittest.mock import Mock
    import json

    from app.config import PROJECT_ROOT
    from app.services.ffprobe import FFprobeService, parse_ffprobe

    movie = tmp_path / "movies"
    shows = tmp_path / "shows"
    cartoons = tmp_path / "cartoons"
    cartoon_films = tmp_path / "cartoons-filmes"
    for root in (movie, shows, cartoons, cartoon_films):
        root.mkdir()
    (movie / "Feature.mkv").write_bytes(b"movie")
    (shows / "Drama").mkdir()
    (shows / "Drama" / "Drama.S01E01.mkv").write_bytes(b"show")
    (cartoons / "Animation").mkdir()
    (cartoons / "Animation" / "Animation.S01E01.mkv").write_bytes(b"cartoon")
    (cartoon_films / "Animated Feature.mkv").write_bytes(b"animated film")

    facts = parse_ffprobe(json.loads((PROJECT_ROOT / "fixtures/ffprobe/progressive.json").read_text()))
    monkeypatch.setattr(FFprobeService, "inspect", Mock(return_value=facts))
    config = Settings(media_backend="filesystem", database_path=tmp_path / "app.sqlite3",
                      movies_root=movie, shows_root=shows, cartoons_root=cartoons,
                      cartoons_filmes_root=cartoon_films)
    with TestClient(create_app(config=config)) as client:
        from tests.test_filesystem_discovery import scan
        assert scan(client).json()["state"] == "completed"
        movies_page = client.get("/movies").text
        animation_page = client.get("/cartoons-filmes").text
        assert "Feature.mkv" not in animation_page
        assert "Animated Feature" in animation_page
        assert "Animated Feature" not in movies_page
        assert "Feature" in movies_page

        shows_page = client.get("/shows").text
        cartoons_page = client.get("/cartoons").text
        assert "Drama" in shows_page and "Animation" not in shows_page
        assert "Animation" in cartoons_page and "Drama" not in cartoons_page
        import re
        cartoon_show = re.search(r'href="/cartoons/([^"]+)"', cartoons_page)
        assert cartoon_show is not None
        assert client.get("/cartoons/" + cartoon_show.group(1)).status_code == 200
        assert client.get("/shows/" + cartoon_show.group(1)).status_code == 404
        assert client.get("/cartoons/incorrect").status_code == 404
