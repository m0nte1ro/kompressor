from app.models.preset import CompressionPreset
from app.models.queue import QueueJob
from app.models.tags import TagAssignment
from app.repositories.database import Database


class SQLitePresetRepository:
    def __init__(self, database: Database, initial: list[CompressionPreset]):
        self.database = database
        with database.transaction() as connection:
            if not connection.execute("SELECT 1 FROM metadata WHERE id = 'presets_initialized'").fetchone():
                connection.executemany("INSERT INTO presets VALUES (?, ?)",
                                       [(p.id, p.model_dump_json()) for p in initial])
                connection.execute("INSERT INTO metadata VALUES ('presets_initialized', 'true')")

    def get_all(self) -> list[CompressionPreset]:
        with self.database.read() as connection:
            return [CompressionPreset.model_validate_json(row[0]) for row in
                    connection.execute("SELECT payload FROM presets ORDER BY rowid")]

    def get_by_id(self, preset_id: str) -> CompressionPreset | None:
        with self.database.read() as connection:
            row = connection.execute("SELECT payload FROM presets WHERE id = ?", (preset_id,)).fetchone()
            return CompressionPreset.model_validate_json(row[0]) if row else None

    def delete(self, preset_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM presets WHERE id = ?", (preset_id,))

    def save(self, preset: CompressionPreset) -> None:
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO presets VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                               (preset.id, preset.model_dump_json()))


class SQLiteTagRepository:
    def __init__(self, database: Database):
        self.database = database

    def transaction(self):
        return self.database.transaction()

    def get_many(self, keys: list[str]) -> dict[str, TagAssignment]:
        result = {}
        with self.database.read() as connection:
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                marks = ','.join('?' for _ in batch)
                for row in connection.execute(f'SELECT id,payload FROM tags WHERE id IN ({marks})', batch):
                    result[row[0]] = TagAssignment.model_validate_json(row[1])
        return result

    def get(self, key: str) -> TagAssignment | None:
        with self.database.read() as connection:
            row = connection.execute("SELECT payload FROM tags WHERE id = ?", (key,)).fetchone()
            return TagAssignment.model_validate_json(row[0]) if row else None

    def save(self, key: str, assignment: TagAssignment) -> None:
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO tags VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                               (key, assignment.model_dump_json()))


class SQLiteQueueRepository:
    def __init__(self, database: Database):
        self.database = database

    def transaction(self):
        return self.database.transaction()

    def get_all(self) -> list[QueueJob]:
        with self.database.read() as connection:
            return [QueueJob.model_validate_json(row[0]) for row in
                    connection.execute("SELECT payload FROM jobs ORDER BY rowid")]

    def add(self, job: QueueJob) -> None:
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO jobs VALUES (?, ?)", (job.id, job.model_dump_json()))

    def save(self, job: QueueJob) -> None:
        with self.database.transaction() as connection:
            connection.execute("UPDATE jobs SET payload = ? WHERE id = ?", (job.model_dump_json(), job.id))

    def remove(self, job_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
