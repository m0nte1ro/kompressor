"""Migrate built-in preset catalogues without rewriting user state."""
import json
from pathlib import Path

from app.config import PROJECT_ROOT


CATALOGUE_MARKER = "preset_catalog_v3"
LEGACY_FIXTURES = (
    "presets-legacy.json",
    "presets-streaming-v1.json",
)


def _load_legacy_presets() -> list[dict]:
    originals = []
    for filename in LEGACY_FIXTURES:
        path: Path = PROJECT_ROOT / "fixtures" / filename
        originals.extend(json.loads(path.read_text(encoding="utf-8"))["presets"])
    return originals


def _was_untouched(stored: dict, original: dict) -> bool:
    return all(stored.get(key) == value for key, value in original.items())


def upgrade_streaming_presets(database, presets):
    originals = _load_legacy_presets()
    with database.transaction() as connection:
        if connection.execute("SELECT 1 FROM metadata WHERE id = ?", (CATALOGUE_MARKER,)).fetchone():
            return
        for original in originals:
            row = connection.execute("SELECT payload FROM presets WHERE id = ?", (original["id"],)).fetchone()
            if not row:
                continue
            stored = json.loads(row[0])
            if stored.get("origin") in (None, "built_in") or _was_untouched(stored, original):
                connection.execute("DELETE FROM presets WHERE id=?", (original["id"],))
        for preset in presets:
            connection.execute("INSERT OR IGNORE INTO presets VALUES (?, ?)", (preset.id, preset.model_dump_json()))
        connection.execute("INSERT INTO metadata VALUES (?, 'true')", (CATALOGUE_MARKER,))
