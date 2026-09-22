"""SQLite transactions shared by preset, tag and queue repositories."""
import sqlite3
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
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            for table in ("presets", "tags", "jobs", "metadata"):
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def transaction(self):
        with self.lock:
            existing = getattr(self.context, "connection", None)
            if existing is not None:
                yield existing
                return
            connection = sqlite3.connect(self.path, timeout=10)
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
