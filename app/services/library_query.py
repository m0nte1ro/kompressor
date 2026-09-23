"""Scoped repository projection, with a small seed/in-memory fallback."""
from app.models.media import MediaLibrary
from app.repositories.base import MediaRepository


def select_library(media: MediaRepository, scope: str | None = None,
                   show_id: str | None = None, file_id: str | None = None) -> MediaLibrary:
    select = getattr(media, 'select_library', None)
    if select is not None:
        return select(scope=scope, show_id=show_id, file_id=file_id)
    library = media.get_library().model_copy(deep=True)
    library.movies = [m for m in library.movies if scope != 'show' and (file_id is None or m.id == file_id)]
    library.shows = [s for s in library.shows if scope != 'movie' and (show_id is None or s.id == show_id)]
    if file_id is not None:
        for show in library.shows:
            for season in show.seasons:
                season.episodes = [e for e in season.episodes if e.id == file_id]
            show.seasons = [s for s in show.seasons if s.episodes]
        library.shows = [s for s in library.shows if s.seasons]
    return library
