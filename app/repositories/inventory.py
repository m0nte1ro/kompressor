"""Indexed inventory queries. Full exports exist only for fixtures and migration checks."""
import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from app.models.inventory import (FileRevision, InventoryState, LibraryFile, OutputArtifact,
                                  ReconciliationResult, ScanSnapshot)
from app.repositories.database import Database
from app.repositories.inventory_views import InventoryViews
from app.repositories.observation_store import observations, store_observation


class InventoryRepository(Protocol):
    views: InventoryViews

    def transaction(self) -> AbstractContextManager[object]: ...
    def get_file(self, file_id: str) -> LibraryFile | None: ...
    def files(self, *, root_id: str | None = None, roots: list[str] | None = None,
              present: bool = False, scope: str | None = None, show_id: str | None = None,
              file_id: str | None = None, path: str | None = None) -> list[LibraryFile]: ...
    def root_state(self, root_id: str, candidates=None) -> InventoryState: ...
    def apply(self, state: InventoryState, snapshot: ScanSnapshot, result: ReconciliationResult) -> None: ...
    def get_artifact(self, artifact_id: str) -> OutputArtifact | None: ...
    def artifacts(self, root_id: str | None = None, job_id: str | None = None) -> list[OutputArtifact]: ...
    def store_artifact(self, artifact: OutputArtifact) -> None: ...
    def store_file(self, record: LibraryFile) -> None: ...
    def attach_probe(self, file_id: str, revision_id: str, observation) -> bool: ...


def show_identity(record: LibraryFile) -> str | None:
    if record.scope != 'show':
        return None
    from app.services.filesystem_scanner import EPISODE
    path = Path(record.relative_path)
    match = EPISODE.search(path.stem)
    if match is None:
        return None
    name = path.parts[0] if len(path.parts) > 1 else path.stem[:match.start()]
    return 'filesystem:' + str(uuid5(NAMESPACE_URL, f'{record.root_id}:show:{name}'))


class SQLiteInventoryRepository:
    key = 'reconciliation_inventory_v1'

    def __init__(self, database: Database):
        self.database = database
        self.views = InventoryViews(database)

    def transaction(self) -> AbstractContextManager[object]:
        return self.database.transaction()

    def files(self, *, root_id=None, roots=None, present=False, scope=None, show_id=None,
              file_id=None, path=None, candidates=None) -> list[LibraryFile]:
        clauses, params = [], []
        for column, value in [('root_id', root_id), ('scope', scope), ('show_id', show_id),
                              ('file_id', file_id), ('relative_path', path)]:
            if value is not None:
                clauses.append(f'{column}=?')
                params.append(value)
        if roots is not None:
            if not roots:
                return []
            clauses.append('root_id IN (' + ','.join('?' for _ in roots) + ')')
            params.extend(roots)
        if candidates is not None:
            if not candidates:
                return []
            media = list({item.media_id for item in candidates})
            paths = list({item.relative_path for item in candidates})
            clauses.append('(media_id IN (' + ','.join('?' for _ in media) + ') OR relative_path IN (' + ','.join('?' for _ in paths) + '))')
            params.extend(media + paths)
        if present:
            clauses.append("presence='present'")
        query = 'SELECT * FROM library_files' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
        with self.database.read() as connection:
            rows = connection.execute(query, params).fetchall()
            facts = observations(connection, [row['observation_id'] for row in rows])
            return [LibraryFile.model_validate({**{k: row[k] for k in row.keys() if k not in ('observation_id', 'show_id')},
                                               'observation': facts[row['observation_id']]}) for row in rows]

    def get_file(self, file_id: str) -> LibraryFile | None:
        rows = self.files(file_id=file_id)
        return rows[0] if rows else None

    def get_revision(self, revision_id: str) -> FileRevision | None:
        with self.database.read() as connection:
            row = connection.execute('SELECT * FROM file_revisions WHERE revision_id=?', (revision_id,)).fetchone()
            return FileRevision(revision_id=revision_id, file_id=row['file_id'],
                                observation=observations(connection, [row['observation_id']])[row['observation_id']]) if row else None

    def root_state(self, root_id: str, candidates=None) -> InventoryState:
        with self.database.read() as connection:
            sequence = connection.execute('SELECT MAX(sequence) FROM scan_runs WHERE root_id=?', (root_id,)).fetchone()[0]
            if candidates is None:
                records = {f.file_id: f for f in self.files(root_id=root_id)}
            else:
                records = {f.file_id: f for start in range(0, len(candidates), 400)
                           for f in self.files(root_id=root_id, candidates=candidates[start:start + 400])}
            return InventoryState(files=records,
                                  artifacts={a.artifact_id: a for a in self.artifacts(root_id)},
                                  scan_sequences={root_id: sequence or 0})

    def store_file(self, record: LibraryFile) -> None:
        with self.database.transaction() as connection:
            key = 'file:' + record.file_id
            store_observation(connection, key, record.observation)
            values = (record.file_id, record.media_id, record.scope, record.root_id, record.relative_path,
                      record.revision_id, key, record.presence, record.last_seen_sequence, show_identity(record))
            connection.execute('INSERT INTO library_files VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET '
                               'media_id=excluded.media_id,scope=excluded.scope,root_id=excluded.root_id,'
                               'relative_path=excluded.relative_path,revision_id=excluded.revision_id,'
                               'observation_id=excluded.observation_id,presence=excluded.presence,'
                               'last_seen_sequence=excluded.last_seen_sequence,show_id=excluded.show_id', values)

    def attach_probe(self, file_id, revision_id, observation) -> bool:
        with self.database.transaction() as connection:
            current = self.get_file(file_id)
            if current is None or current.revision_id != revision_id or current.presence != 'present':
                return False
            if current.observation.model_copy(update={'probe': None, 'hardlinks': observation.hardlinks}) != observation.model_copy(update={'probe': None}):
                return False
            current.observation = observation
            self.store_file(current)
            revision = self.get_revision(revision_id)
            if revision is not None:
                store_observation(connection, 'revision:' + revision_id,
                                  revision.observation.model_copy(update={'probe': observation.probe}))
            return True

    def store_revision(self, revision: FileRevision) -> None:
        with self.database.transaction() as connection:
            key = 'revision:' + revision.revision_id
            store_observation(connection, key, revision.observation)
            connection.execute('INSERT INTO file_revisions(revision_id,file_id,observation_id) VALUES (?,?,?)',
                               (revision.revision_id, revision.file_id, key))

    def apply(self, state: InventoryState, snapshot: ScanSnapshot, result: ReconciliationResult) -> None:
        with self.database.transaction() as connection:
            # Release paths together so path swaps obey the unique present-path constraint.
            touched = list(dict.fromkeys(result.created + result.revised + result.moved + result.unchanged + result.missing))
            connection.executemany("UPDATE library_files SET presence='missing' WHERE file_id=?", [(key,) for key in touched])
            for revision in state.revisions.values():
                self.store_revision(revision)
            for key in touched:
                self.store_file(state.files[key])
            connection.execute('INSERT INTO scan_runs(root_id,sequence,status) VALUES (?,?,?)',
                               (snapshot.root_id, snapshot.sequence, snapshot.status))
            connection.executemany('INSERT INTO reconciliation_issues VALUES (?,?,?,?,?,?,?)',
                                   [(snapshot.root_id, snapshot.sequence, i, issue.path, issue.reason,
                                     issue.fingerprint_needed, json.dumps(issue.candidate_file_ids))
                                    for i, issue in enumerate(result.issues)])

    def artifacts(self, root_id=None, job_id=None) -> list[OutputArtifact]:
        clauses, params = [], []
        for column, value in [('root_id', root_id), ('job_id', job_id)]:
            if value is not None:
                clauses.append(f'{column}=?')
                params.append(value)
        with self.database.read() as connection:
            rows = connection.execute('SELECT * FROM artifacts' + (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params)
            return [self._artifact(dict(row)) for row in rows]

    @staticmethod
    def _artifact(row: dict) -> OutputArtifact:
        row['source'] = {'file_id': row.pop('file_id'), 'revision_id': row.pop('revision_id')}
        row['preset'] = json.loads(row['preset'])
        return OutputArtifact.model_validate(row)

    def get_artifact(self, artifact_id: str) -> OutputArtifact | None:
        with self.database.read() as connection:
            row = connection.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
            return self._artifact(dict(row)) if row else None

    def store_artifact(self, artifact: OutputArtifact) -> None:
        with self.database.transaction() as connection:
            connection.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(artifact_id) DO UPDATE SET status=excluded.status',
                               (artifact.artifact_id, artifact.job_id, artifact.source.file_id, artifact.source.revision_id,
                                artifact.root_id, artifact.relative_path, artifact.preset.model_dump_json(), artifact.mode, artifact.status))

    def load(self) -> InventoryState:
        """Explicit diagnostic export for fixtures/tests; never used by runtime services."""
        with self.database.read() as connection:
            rows = connection.execute('SELECT * FROM file_revisions').fetchall()
            facts = observations(connection, [row['observation_id'] for row in rows])
            return InventoryState(files={f.file_id: f for f in self.files()},
                revisions={r['revision_id']: FileRevision(revision_id=r['revision_id'], file_id=r['file_id'],
                           observation=facts[r['observation_id']]) for r in rows},
                artifacts={a.artifact_id: a for a in self.artifacts()},
                scan_sequences=dict(connection.execute('SELECT root_id,MAX(sequence) FROM scan_runs GROUP BY root_id')))

    def import_legacy(self, state: InventoryState) -> None:
        """One-time schema migration only; invalid references fail the enclosing transaction."""
        for key, file in state.files.items():
            revision = state.revisions.get(file.revision_id)
            if key != file.file_id or revision is None or revision.file_id != key:
                raise ValueError('Invalid legacy current revision relationship.')
            obs = file.observation
            if (file.root_id, file.relative_path, file.media_id, file.scope) != (obs.root_id, obs.relative_path, obs.media_id, obs.scope):
                raise ValueError('Inconsistent legacy file observation.')
        for key, revision in state.revisions.items():
            if key != revision.revision_id or revision.file_id not in state.files:
                raise ValueError('Invalid legacy revision identity.')
            self.store_revision(revision)
        for file in state.files.values():
            self.store_file(file)
        for key, artifact in state.artifacts.items():
            revision = state.revisions.get(artifact.source.revision_id)
            if key != artifact.artifact_id or revision is None or revision.file_id != artifact.source.file_id:
                raise ValueError('Invalid legacy artifact reference.')
            if any(f.root_id == artifact.root_id and f.relative_path == artifact.relative_path for f in state.files.values()):
                raise ValueError('Legacy artifact overlaps library file.')
            self.store_artifact(artifact)
        with self.database.transaction() as connection:
            connection.executemany('INSERT INTO scan_runs(root_id,sequence,status) VALUES (?,?,?)',
                                   [(root, seq, 'migrated') for root, seq in state.scan_sequences.items()])
