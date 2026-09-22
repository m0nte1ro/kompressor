"""Shared media lookup and policy evaluation for the API, views and scheduler."""
from dataclasses import dataclass

from app.models.media import spatial_resolution
from app.services.errors import NotFound
from app.models.media import Episode, Movie
from app.models.policy import EligibilityResult
from app.models.preset import CompressionPreset
from app.repositories.base import MediaRepository, PresetRepository
from app.services.policy import PolicyEngine
from app.models.tags import QualityFloor, TagTarget
from app.services.tags import TagService


@dataclass
class MediaEntry:
    item: Movie | Episode
    scope: str
    name: str
    tags: list[str]
    show_id: str | None = None
    quality_floor: QualityFloor | None = None


class CatalogService:
    def __init__(self, media: MediaRepository, presets: PresetRepository,
                 policy: PolicyEngine, tagger: TagService | None = None):
        self.media = media
        self.presets = presets
        self.policy = policy
        self.tagger = tagger

    def library(self):
        return self.tagger.library() if self.tagger else self.media.get_library()

    def entries(self) -> list[MediaEntry]:
        library = self.library()
        entries = [MediaEntry(m, "movie", m.name, m.tags) for m in library.movies]
        for show in library.shows:
            for season in sorted(show.seasons, key=lambda s: s.season):
                for episode in sorted(season.episodes, key=lambda e: e.episode):
                    tags = list(dict.fromkeys([*show.tags, *season.tags, *episode.tags]))
                    code = f"S{episode.season:02}E{episode.episode:02}"
                    name = f"{show.name} · {code} · {episode.title or 'Untitled'}"
                    entries.append(MediaEntry(episode, "show", name, tags, show.id))
        if self.tagger:
            for entry in entries:
                target = TagTarget(kind="movie" if entry.scope == "movie" else "episode", id=entry.item.id)
                floor = self.tagger.describe(target)["effective_quality_floor"]
                entry.quality_floor = QualityFloor.model_validate(floor) if floor else None
        return entries

    def find(self, media_id: str, scope: str) -> MediaEntry:
        for entry in self.entries():
            if entry.item.id == media_id and entry.scope == scope:
                return entry
        raise NotFound("Movie not found." if scope == "movie" else "Episode not found.")

    def preset(self, preset_id: str) -> CompressionPreset:
        preset = self.presets.get_by_id(preset_id)
        if preset is None:
            raise NotFound("Preset not found.")
        return preset

    def evaluate(self, entry: MediaEntry, preset: CompressionPreset,
                 preserve_audio: bool | None = None,
                 preserve_subtitles: bool = True) -> EligibilityResult:
        return self.policy.evaluate(
            item=entry.item, scope=entry.scope, preset=preset,
            effective_tags=entry.tags, preserve_audio=preserve_audio,
            preserve_subtitles=preserve_subtitles,
            quality_floor=entry.quality_floor,
        )

    def preview(self, entry: MediaEntry) -> tuple[CompressionPreset | None, EligibilityResult]:
        """Use the first eligible scoped preset, or expose the first one's blockers."""
        presets = [p for p in self.presets.get_all() if p.scope == entry.scope and p.enabled]
        if not presets:
            return None, EligibilityResult(
                eligible=False, reasons=[f"No enabled {entry.scope} presets. Create or enable one in Settings."],
                warnings=[], preset_id="", preset_name="No enabled preset", backend="", destination_codec="",
                source_size=entry.item.size, estimated_output_size=None, estimated_saving=None,
                estimated_saving_percent=None, preserve_audio=True, preserve_subtitles=True,
            )
        presets.sort(key=lambda p: (spatial_resolution(entry.item) not in p.source_resolutions,
                                    bool(entry.item.hdr) and p.hdr_support == "sdr_only"))
        fallback = None
        for preset in presets:
            result = self.evaluate(entry, preset)
            fallback = fallback or (preset, result)
            if result.eligible:
                return preset, result
        assert fallback is not None
        return fallback

    def summary(self) -> dict:
        library = self.library()
        episodes = [e for s in library.shows for season in s.seasons for e in season.episodes]
        return {"movies": len(library.movies), "shows": len(library.shows), "episodes": len(episodes),
                "movie_bytes": sum(m.size for m in library.movies), "show_bytes": sum(e.size for e in episodes)}

    def previews(self, scope: str, show_id: str | None = None) -> list[dict]:
        result = []
        for entry in self.entries():
            if entry.scope != scope or (show_id is not None and entry.show_id != show_id):
                continue
            preset, eligibility = self.preview(entry)
            result.append({"entry": entry, "item": entry.item, "preset": preset, "eligibility": eligibility})
        return result

    def shows(self) -> list[dict]:
        cards = []
        for show in self.library().shows:
            episodes = [e for season in show.seasons for e in season.episodes]
            cards.append({"show": show, "count": len(episodes), "size": sum(e.size for e in episodes)})
        return cards

    def show_detail(self, show_id: str) -> dict:
        show = next((s for s in self.library().shows if s.id == show_id), None)
        if show is None:
            raise NotFound("Show not found.")
        return {"show": show, "rows": self.previews("show", show_id)}
