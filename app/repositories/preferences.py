import json

from app.models.preferences import LibraryPaths
from app.repositories.database import Database


PREFERENCES_ID = "library_paths"


class SQLitePreferencesRepository:
    def __init__(self, database: Database):
        self.database = database

    def get_library_paths(self) -> LibraryPaths:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM metadata WHERE id = ?",
                (PREFERENCES_ID,),
            ).fetchone()
            if row is None:
                return LibraryPaths()
            return LibraryPaths.model_validate_json(row[0])

    def save_library_paths(self, paths: LibraryPaths) -> LibraryPaths:
        payload = paths.model_dump_json()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (PREFERENCES_ID, payload),
            )
        return paths
