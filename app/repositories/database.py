"""SQLite transactions shared by preset, tag and queue repositories."""
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        self.context = local()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            for table in ("presets", "tags", "jobs", "metadata"):
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            if version < 2:
                from app.models.inventory import InventoryState
                from app.repositories.inventory import SQLiteInventoryRepository
                from app.repositories.inventory_schema import SCHEMA
                try:
                    for statement in SCHEMA:
                        connection.execute(statement)
                    legacy = connection.execute("SELECT payload FROM metadata WHERE id=?",
                                                (SQLiteInventoryRepository.key,)).fetchone()
                    if legacy:
                        raw = json.loads(legacy[0])
                        if not isinstance(raw, dict) or raw.keys() - InventoryState.model_fields.keys():
                            raise ValueError("Unrecognized legacy inventory structure.")
                        SQLiteInventoryRepository(self).import_legacy(InventoryState.model_validate(raw))
                        connection.execute("DELETE FROM metadata WHERE id=?", (SQLiteInventoryRepository.key,))
                    if connection.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("Invalid inventory foreign-key references.")
                    connection.execute("PRAGMA user_version = 2")
                except Exception as error:
                    raise RuntimeError(f"Inventory schema migration failed; database unchanged: {error}") from error
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def transaction(self):
        with self.lock:
            existing = getattr(self.context, "connection", None)
            if existing is not None:
                yield existing
                return
            connection = self._connect()
            self.context.connection = connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self.context.connection = None
                connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def read(self):
        """Snapshot reads do not acquire the writer lock or reserve a write transaction."""
        existing = getattr(self.context, "connection", None)
        if existing is not None:
            yield existing
            return
        connection = self._connect()
        self.context.connection = connection
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            self.context.connection = None
            connection.rollback()
            connection.close()
