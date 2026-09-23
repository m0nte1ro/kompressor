"""Projection of reconciled probe facts into the existing UI/policy domain models."""
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.models.inventory import LibraryFile
from app.models.media import AudioTrack, Episode, HDRType, MediaLibrary, Movie, Season, Show
from app.models.probe import StreamFacts
from app.models.tags import TagTarget
from app.repositories.inventory import InventoryRepository
from app.services.errors import NotFound
from app.services.filesystem_scanner import EPISODE, root_identity


def display_name(value: str) -> str:
    return re.sub(r'[._]+', ' ', value).strip(' -')


def source_video_bitrate(record: LibraryFile, probe, video: StreamFacts | None) -> tuple[int | None, bool]:
    """Return stream bitrate, or a conservative average derived from container/file facts."""
    if video is None:
        return None, False
    if video.bitrate is not None:
        return video.bitrate, False
    if probe is None or probe.duration_seconds is None or probe.duration_seconds <= 0:
        return None, False
    total = probe.container_bitrate
    if total is None and record.observation.size > 0:
        total = int(record.observation.size * 8 / probe.duration_seconds)
    if total is None or total <= 0:
        return None, False
    known_other = sum(
        stream.bitrate or 0 for stream in probe.streams if stream.index != video.index
    )
    estimated = total - known_other
    return (int(estimated), True) if estimated > 0 else (None, False)


def media_fields(record: LibraryFile, root: Path) -> dict:
    probe = record.observation.probe
    videos = [s for s in probe.streams if s.kind == 'video' and not s.dispositions.get('attached_pic')] if probe else []
    video: StreamFacts | None = videos[0] if videos else None
    hdr: HDRType | None = 'unknown'
    if video and video.hdr:
        classification = video.hdr.classify()
        if classification.dolby_vision:
            hdr = 'dolby_vision'
        elif classification.hdr10plus:
            hdr = 'hdr10plus'
        elif classification.base == 'sdr':
            hdr = None
        elif classification.base == 'hdr10':
            hdr = 'hdr10'
        elif classification.base == 'hlg':
            hdr = 'hlg'
    scan = video.scan_type if video else 'unknown'
    res = video.resolution_class if video else None
    resolution = f'{res}{"i" if scan in {"interlaced", "mixed"} else "p" if scan == "progressive" else ""}' if res else 'Unknown'
    audio = [AudioTrack(codec=s.codec, channels=s.channels, bitrate=s.bitrate, language=s.language,
                        title=s.title, stream_index=s.index, channel_layout=s.channel_layout,
                        dispositions=s.dispositions) for s in probe.streams if s.kind == 'audio'] if probe else []
    video_bitrate, video_bitrate_estimated = source_video_bitrate(record, probe, video)
    return dict(id=record.file_id, media_id=record.media_id, revision_id=record.revision_id,
                path=str(root / record.relative_path), size=record.observation.size,
                width=video.width if video else None, height=video.height if video else None,
                resolution=resolution, video_codec=video.codec if video else 'unknown',
                video_bitrate=video_bitrate, video_bitrate_estimated=video_bitrate_estimated,
                duration_seconds=probe.duration_seconds if probe else None, hdr=hdr,
                interlaced=True if scan in {'interlaced', 'mixed'} else False if scan == 'progressive' else None,
                hardlinks=record.observation.hardlinks, audio=audio, probe=probe,
                processing_supported=False)


class FilesystemMediaRepository:
    def __init__(self, inventory: InventoryRepository, roots: Callable[[], dict[str, Path]]):
        self.inventory = inventory
        self.roots = roots

    def root_ids(self) -> list[str]:
        return [root_identity('movie' if scope == 'movie' else 'show', root) for scope, root in self.roots().items()]

    def summary(self) -> dict:
        return self.inventory.views.summary(self.root_ids())

    def show_cards(self) -> list[dict]:
        cards: list[dict] = []
        for row in self.inventory.views.shows(self.root_ids()):
            relative = Path(row['path'])
            match = EPISODE.search(relative.stem)
            name = relative.parts[0] if len(relative.parts) > 1 else relative.stem[:match.start()] if match else relative.stem
            cards.append({'show': Show(id=row['show_id'], name=display_name(name)),
                          'count': row['count'], 'size': row['size']})
        return sorted(cards, key=lambda card: card['show'].name.casefold())

    def tag_ancestors(self, target: TagTarget):
        paths = self.inventory.views.show_paths(self.root_ids(), target.id,
                                                 first_only=target.kind == 'show')
        with closing(paths):
            for value in paths:
                relative = Path(value)
                match = EPISODE.search(relative.stem)
                if match is None or target.kind == 'season' and int(match[1]) != target.season:
                    continue
                name = relative.parts[0] if len(relative.parts) > 1 else relative.stem[:match.start()]
                show = Show(id=target.id, name=display_name(name))
                show_target = TagTarget(kind='show', id=target.id)
                chain: list[tuple[TagTarget, Show | Season, str]] = [(show_target, show, show.name)]
                if target.kind == 'season':
                    season = Season(season=int(match[1]))
                    chain.append((target, season, f'{show.name} · Season {season.season}'))
                return chain
        raise NotFound('Tag target not found.')

    def get_library(self) -> MediaLibrary:
        return self.select_library()

    def select_library(self, scope: str | None = None, show_id: str | None = None, file_id: str | None = None) -> MediaLibrary:
        roots = self.roots()
        locations = {root_identity('movie' if scope == 'movie' else 'show', path): path for scope, path in roots.items()}
        library = MediaLibrary()
        shows: dict[str, Show] = {}
        for record in self.inventory.files(roots=list(locations), present=True, scope=scope, show_id=show_id, file_id=file_id):
            if record.presence != 'present' or record.root_id not in locations:
                continue
            fields = media_fields(record, locations[record.root_id])
            relative = Path(record.relative_path)
            if record.scope == 'movie':
                name = relative.stem
                year = re.search(r'\b((?:19|20)\d{2})\b', name)
                source = 'REMUX' if 'remux' in relative.as_posix().lower() else None
                library.movies.append(Movie(name=display_name(name), year=int(year[1]) if year else None,
                                            source=source, **fields))
            else:
                match = EPISODE.search(relative.stem)
                if match is None:
                    continue
                name = relative.parts[0] if len(relative.parts) > 1 else relative.stem[:match.start()]
                show_id = 'filesystem:' + str(uuid5(NAMESPACE_URL, f'{record.root_id}:show:{name}'))
                show = shows.setdefault(show_id, Show(id=show_id, name=display_name(name)))
                number = int(match[1])
                season = next((s for s in show.seasons if s.season == number), None)
                if season is None:
                    season = Season(season=number)
                    show.seasons.append(season)
                season.episodes.append(Episode(season=number, episode=int(match[2]),
                                               title=display_name(relative.stem), **fields))
        library.shows = sorted(shows.values(), key=lambda s: s.name.casefold())
        library.movies.sort(key=lambda m: m.name.casefold())
        return library
