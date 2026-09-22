"""One-time additive upgrade. User edits and queued preset snapshots are retained."""
import json

from app.config import PROJECT_ROOT


def upgrade_streaming_presets(database, presets):
    originals = json.loads((PROJECT_ROOT / "fixtures/presets-legacy.json").read_text())["presets"]
    with database.transaction() as connection:
        if connection.execute("SELECT 1 FROM metadata WHERE id = 'streaming_presets_v1'").fetchone():
            return
        for original in originals:
            row = connection.execute("SELECT payload FROM presets WHERE id = ?", (original["id"],)).fetchone()
            if not row:
                continue
            stored = json.loads(row[0])
            # Extra encoder/applicability fields indicate a deliberate newer edit.
            if all(stored.get(key) == value for key, value in original.items()) and not stored.get("source_resolutions") and stored.get("rate_control", "abr") == "abr":
                stored["enabled"] = False
                connection.execute("UPDATE presets SET payload=? WHERE id=?", (json.dumps(stored), original["id"]))
        for preset in presets:
            connection.execute("INSERT OR IGNORE INTO presets VALUES (?, ?)", (preset.id, preset.model_dump_json()))
        connection.execute("INSERT INTO metadata VALUES ('streaming_presets_v1', 'true')")
