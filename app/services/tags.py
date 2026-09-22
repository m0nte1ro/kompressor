from typing import cast

from app.services.errors import NotFound
from app.models.tags import QualityFloor, TagAssignment, TagName, TagTarget, TagUpdate
from app.repositories.base import MediaRepository, TagRepository


class TagService:
    def __init__(self, media: MediaRepository, repository: TagRepository):
        self.media = media
        self.repository = repository

    def _chain(self, target: TagTarget):
        library = self.media.get_library()
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

    def assignment(self, target: TagTarget, item) -> TagAssignment:
        return self.repository.get(target.key()) or TagAssignment(tags=item.tags)

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

    def library(self):
        library = self.media.get_library().model_copy(deep=True)
        with self.repository.transaction():
            for movie in library.movies:
                movie.tags = [str(tag) for tag in self.assignment(TagTarget(kind="movie", id=movie.id), movie).tags]
            for show in library.shows:
                show.tags = [str(tag) for tag in self.assignment(TagTarget(kind="show", id=show.id), show).tags]
                for season in show.seasons:
                    season.tags = [str(tag) for tag in self.assignment(TagTarget(kind="season", id=show.id, season=season.season), season).tags]
                    for episode in season.episodes:
                        episode.tags = [str(tag) for tag in self.assignment(TagTarget(kind="episode", id=episode.id), episode).tags]
        return library

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
                self.repository.save(target.key(), TagAssignment(tags=list(cast(list[TagName], tags)), quality_floor=floor))
            return [self.describe(target) for target, _ in resolved]
