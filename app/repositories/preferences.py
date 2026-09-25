from app.models.preferences import LibraryPaths, WorkerSettings
from app.repositories.database import Database


PREFERENCES_ID = "library_paths"
WORKER_SETTINGS_ID = "worker_settings"


class SQLitePreferencesRepository:
    def __init__(self, database: Database, defaults: LibraryPaths | None = None,
                 worker_defaults: WorkerSettings | None = None):
        self.database = database
        self.defaults = defaults or LibraryPaths()
        self.worker_defaults = worker_defaults or WorkerSettings()

    def get_library_paths(self) -> LibraryPaths:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT payload FROM metadata WHERE id = ?",
                (PREFERENCES_ID,),
            ).fetchone()
            if row is None:
                return self.defaults.model_copy()
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


    def get_worker_settings(self) -> WorkerSettings:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT payload FROM metadata WHERE id = ?",
                (WORKER_SETTINGS_ID,),
            ).fetchone()
            if row is None:
                return self.worker_defaults.model_copy(deep=True)
            return WorkerSettings.model_validate_json(row[0])

    def save_worker_settings(self, settings: WorkerSettings) -> WorkerSettings:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (WORKER_SETTINGS_ID, settings.model_dump_json()),
            )
        return settings
