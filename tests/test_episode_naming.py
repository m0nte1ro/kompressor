"""Regression cases for the real filenames encountered during library scans."""
import pytest

from app.services.filesystem_scanner import FilesystemScanner, semantic_key
from app.models.inventory import InventoryState


@pytest.mark.parametrize(("path", "expected"), [
    ("The Grand Tour (2016) [tmdbid-67557]/The Grand Tour S01 E12 Web-Rip 1080p.avi",
     "The Grand Tour (2016) [tmdbid-67557]:S1E12"),
    ("The Vampire Diaries [tmdbid-18165]/The Vampire Diaries - 01x05 You're Undead to Me.mkv",
     "The Vampire Diaries [tmdbid-18165]:S1E5"),
    ("The Vampire Diaries [tmdbid-18165]/The Vampire Diaries 08x16 I Was Feeling Epic.mkv",
     "The Vampire Diaries [tmdbid-18165]:S8E16"),
    ("Top Gear [tmdbid-45]/top_gear.20x01.720p_hdtv_x264-fov.mkv",
     "Top Gear [tmdbid-45]:S20E1"),
    ("Top Gear [tmdbid-45]/Top.Gear.S20E01.mkv",
     "Top Gear [tmdbid-45]:S20E1"),
])
def test_common_episode_names_resolve_to_one_semantic_identity(path, expected):
    assert semantic_key("show", path) == expected


@pytest.mark.parametrize("name", [
    "episode without numbers.mkv",
    "Nothingfoo01x05bar.mkv",
    "Movie.2026.1080p.mkv",
])
def test_unrecognized_names_are_not_guessed(name):
    assert semantic_key("show", "Test Series/" + name) is None


def test_scan_collects_spaced_and_x_style_episodes(tmp_path):
    root = tmp_path / "shows"
    show = root / "The Vampire Diaries"
    show.mkdir(parents=True)
    for name in ["The Vampire Diaries - 01x05 You're Undead to Me.mkv",
                 "The Vampire Diaries S01 E06 Episode.mkv"]:
        (show / name).write_bytes(b"not probed")
    snapshot, errors = FilesystemScanner().scan(root, "show", InventoryState())
    assert snapshot.status == "complete"
    assert not errors
    assert len(snapshot.files) == 2
    assert {item.media_id for item in snapshot.files} == {
        "filesystem:" + str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL,
          f"{snapshot.root_id}:The Vampire Diaries:S1E5")),
        "filesystem:" + str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL,
          f"{snapshot.root_id}:The Vampire Diaries:S1E6")),
    }
