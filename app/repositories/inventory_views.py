"""Aggregate library views: no episode probes are loaded to count shows or bytes."""
from app.repositories.database import Database


class InventoryViews:
    def __init__(self, database: Database):
        self.database = database

    def summary(self, roots: list[str]) -> dict:
        result = dict(movies=0, shows=0, episodes=0, movie_bytes=0, show_bytes=0)
        if not roots:
            return result
        with self.database.read() as connection:
            rows = connection.execute('''SELECT f.scope, COUNT(*) AS count, SUM(o.size) AS bytes,
                COUNT(DISTINCT f.show_id) AS shows FROM library_files f
                JOIN observations o USING(observation_id) WHERE f.presence='present' AND f.root_id IN ('''
                + ','.join('?' for _ in roots) + ') GROUP BY f.scope', roots)
            for row in rows:
                if row['scope'] == 'movie':
                    result.update(movies=row['count'], movie_bytes=row['bytes'])
                else:
                    result.update(episodes=row['count'], show_bytes=row['bytes'], shows=row['shows'])
        return result

    def shows(self, roots: list[str]) -> list[dict]:
        if not roots:
            return []
        with self.database.read() as connection:
            return [dict(row) for row in connection.execute('''SELECT f.show_id, MIN(f.relative_path) AS path,
                COUNT(*) AS count, SUM(o.size) AS size FROM library_files f
                JOIN observations o USING(observation_id) WHERE f.presence='present'
                AND f.show_id IS NOT NULL AND f.root_id IN (''' + ','.join('?' for _ in roots)
                + ') GROUP BY f.show_id', roots)]
