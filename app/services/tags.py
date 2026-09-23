from typing import cast
from app.services.library_query import select_library
from app.models.media import Movie, Episode, Show, Season

TaggedItem = Movie | Episode | Show | Season

from app.services.errors import NotFound
from app.models.tags import QualityFloor, TagAssignment, TagName, TagTarget, TagUpdate
from app.repositories.base import MediaRepository, TagRepository


class TagService:
    def __init__(self, media: MediaRepository, repository: TagRepository):
        self.media = media
        self.repository = repository

    def _chain(self, target: TagTarget):
        library = select_library(self.media,
            scope="movie" if target.kind == "movie" else "show",
            show_id=target.id if target.kind in {"show", "season"} else None,
            file_id=target.id if target.kind in {"movie", "episode"} else None)
        if target.kind == "movie":
            for movie in library.movies:
                if movie.id == target.id:
                    return [(target, movie, movie.name)]
        for show in library.shows:
            show_target = TagTarget(kind="show", id=show.id)
            chain = [(show_target, show, show.name)]
            if target.kind == "show" and show.id == target.id:
                return chain
            for season in show.seasons:
                season_target = TagTarget(kind="season", id=show.id, season=season.season)
                season_chain = [*chain, (season_target, season, f"{show.name} · Season {season.season}")]
                if target == season_target:
                    return season_chain
                for episode in season.episodes:
                    if target.kind == "episode" and episode.id == target.id:
                        return [*season_chain, (target, episode, f"{show.name} · S{episode.season:02}E{episode.episode:02}")]
        raise NotFound("Tag target not found.")

    @staticmethod
    def _key(target: TagTarget, item) -> str:
        media_id = getattr(item, "media_id", None)
        if media_id:
            logical = target.model_copy(update={"id": media_id})
            return "filesystem:" + logical.model_dump_json()
        return target.key()

    def assignment(self, target: TagTarget, item) -> TagAssignment:
        return self.repository.get(self._key(target, item)) or TagAssignment(tags=item.tags)

    def describe(self, target: TagTarget) -> dict:
        with self.repository.transaction():
            chain = self._chain(target)
            assignments = [(name, self.assignment(key, item)) for key, item, name in chain]
            floors = [assignment.quality_floor for _, assignment in assignments if assignment.quality_floor]
            floor = QualityFloor(
                minimum_video_bitrate=max(f.minimum_video_bitrate for f in floors),
                minimum_height=max(f.minimum_height for f in floors),
            ) if floors else None
            return {
                "target": target.model_dump(), "name": chain[-1][2],
                "direct": assignments[-1][1].model_dump(),
                "inherited": [{"source": name, **assignment.model_dump()} for name, assignment in assignments[:-1]],
                "effective_tags": list(dict.fromkeys(tag for _, a in assignments for tag in a.tags)),
                "effective_quality_floor": floor.model_dump() if floor else None,
            }

    def decorate(self, library):
        """Load assignments once and propagate floors in a single traversal."""
        chains: list[list[tuple[TagTarget, TaggedItem]]] = []
        for movie in library.movies:
            chains.append([(TagTarget(kind="movie", id=movie.id), movie)])
        for show in library.shows:
            chain: list[tuple[TagTarget, TaggedItem]] = [(TagTarget(kind="show", id=show.id), show)]
            chains.append(chain)
            for season in show.seasons:
                season_chain = [*chain, (TagTarget(kind="season", id=show.id, season=season.season), season)]
                chains.append(season_chain)
                for episode in season.episodes:
                    chains.append([*season_chain, (TagTarget(kind="episode", id=episode.id), episode)])
        keys = list(dict.fromkeys(self._key(t, item) for chain in chains for t, item in chain))
        stored = self.repository.get_many(keys)
        assignments = {}
        for chain in chains:
            for target, item in chain:
                key = self._key(target, item)
                assignments.setdefault(key, stored.get(key) or TagAssignment.model_validate({"tags": item.tags}))
        floors = {}
        for chain in chains:
            target, item = chain[-1]
            item.tags = [str(tag) for tag in assignments[self._key(target, item)].tags]
            inherited = [assignments[self._key(t, i)].quality_floor for t, i in chain]
            active = [floor for floor in inherited if floor is not None]
            if active and target.kind in {"movie", "episode"}:
                floors[target.id] = QualityFloor(minimum_video_bitrate=max(f.minimum_video_bitrate for f in active),
                                               minimum_height=max(f.minimum_height for f in active))
        return library, floors

    def library(self):
        return self.decorate(self.media.get_library().model_copy(deep=True))[0]

    def update(self, request: TagUpdate) -> list[dict]:
        with self.repository.transaction():
            # Resolve all targets before writing; an invalid bulk request changes nothing.
            targets = {t.key(): t for t in request.targets}.values()
            resolved = [(t, self._chain(t)[-1][1]) for t in targets]
            for target, item in resolved:
                current = self.assignment(target, item)
                if request.operation == "replace":
                    tags, floor = list(request.tags), request.quality_floor
                elif request.operation == "add":
                    tags = list(dict.fromkeys([*current.tags, *request.tags]))
                    floor = request.quality_floor if "Quality Floor" in request.tags else current.quality_floor
                else:
                    tags = [tag for tag in current.tags if tag not in request.tags]
                    floor = current.quality_floor if "Quality Floor" in tags else None
                self.repository.save(self._key(target, item), TagAssignment(tags=list(cast(list[TagName], tags)), quality_floor=floor))
            return [self.describe(target) for target, _ in resolved]
