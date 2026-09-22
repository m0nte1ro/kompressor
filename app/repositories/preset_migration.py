"""Migrate built-in preset catalogues without rewriting user state."""
import json
from pathlib import Path

from app.config import PROJECT_ROOT
from app.models.preset import CompressionPreset


CATALOGUE_MARKER = "preset_catalog_v8"
CURRENT_SOURCE_RESOLUTIONS = ["480p", "576p", "720p", "1080p", "2160p"]
PRESERVE_AUDIO_BUILT_INS = {
    "movie-preserve-quality",
    "movie-streaming-quality",
    "show-preserve-quality",
    "show-streaming-quality",
}
HDR_PRESERVE_BUILT_INS = PRESERVE_AUDIO_BUILT_INS | {"show-streaming-efficient-audio"}
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
    normalized = CompressionPreset.model_validate(original).model_dump(mode="json")
    return all(stored.get(key) == value for key, value in normalized.items())


def _normalize_current_built_ins(connection, presets):
    resolution_map = {
        "preserve": "keep",
        "max_2160p": "max_2160p",
        "max_1080p": "max_1080p",
        "max_720p": "max_720p",
        "max_576p": "max_576p",
        "max_480p": "max_480p",
    }
    for preset in presets:
        row = connection.execute("SELECT payload FROM presets WHERE id = ?", (preset.id,)).fetchone()
        if not row:
            continue
        stored = json.loads(row[0])
        changed = False
        if "resolution_policy" in stored and "target_resolution" not in stored:
            stored["target_resolution"] = resolution_map.get(stored.pop("resolution_policy"), "keep")
            changed = True
        if stored.get("source_resolutions") == ["480p", "720p", "1080p", "2160p"]:
            stored["source_resolutions"] = CURRENT_SOURCE_RESOLUTIONS
            changed = True
        if (
            preset.id in {"movie-streaming-quality", "show-streaming-quality"}
            and stored.get("audio_policy") == "preserve"
            and stored.get("audio_conversion_policy") == "efficient"
            and stored.get("target_audio_bitrate") == 640000
        ):
            stored["audio_conversion_policy"] = "preserve"
            stored["target_audio_bitrate"] = None
            changed = True
        if preset.id in PRESERVE_AUDIO_BUILT_INS and stored.get("origin", "built_in") != "custom":
            if (
                stored.get("audio_policy") != "preserve"
                or stored.get("preserve_audio_by_default") is not True
                or stored.get("audio_conversion_policy") != "preserve"
                or stored.get("target_audio_bitrate") is not None
            ):
                stored["audio_policy"] = "preserve"
                stored["preserve_audio_by_default"] = True
                stored["audio_conversion_policy"] = "preserve"
                stored["target_audio_bitrate"] = None
                changed = True
        if preset.id == "show-streaming-efficient-audio" and stored.get("origin", "built_in") != "custom":
            if stored.get("audio_policy") != "efficient" or stored.get("audio_conversion_policy") != "efficient":
                stored["audio_policy"] = "efficient"
                stored["preserve_audio_by_default"] = False
                stored["audio_conversion_policy"] = "efficient"
                stored["target_audio_bitrate"] = stored.get("target_audio_bitrate") or 640000
                changed = True
        if preset.id in HDR_PRESERVE_BUILT_INS and stored.get("origin", "built_in") != "custom":
            if (
                stored.get("hdr_support") != "hdr10_experimental"
                or stored.get("hdr_policy") != "preserve_source"
                or stored.get("preserve_hdr_metadata") is not True
            ):
                stored["hdr_support"] = "hdr10_experimental"
                stored["hdr_policy"] = "preserve_source"
                stored["preserve_hdr_metadata"] = True
                changed = True
        if changed:
            connection.execute("UPDATE presets SET payload=? WHERE id=?", (json.dumps(stored), preset.id))


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
        _normalize_current_built_ins(connection, presets)
        connection.execute("INSERT INTO metadata VALUES (?, 'true')", (CATALOGUE_MARKER,))
