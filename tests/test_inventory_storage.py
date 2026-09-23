"""Migration and query-count regressions using fixture observations only."""
import json
import sqlite3
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.container import build_media_processor
from app.main import create_app
from app.models.inventory import FileObservation, ScanSnapshot
from app.models.probe import MediaProbeResult
from app.models.tags import TagTarget
from app.repositories.database import Database
from app.repositories.discovery_fixture import read_snapshot
from app.repositories.inventory import SQLiteInventoryRepository
from app.services.filesystem_scanner import root_identity
from app.services.reconciliation import ReconciliationService


def legacy_database(path, payload):
    with sqlite3.connect(path) as connection:
        for table in ('presets', 'tags', 'jobs', 'metadata'):
            connection.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            connection.execute(f'INSERT INTO {table} VALUES (?,?)', ('untouched', '{"keep": true}'))
        connection.execute('INSERT INTO metadata VALUES (?,?)', ('reconciliation_inventory_v1', payload))
        connection.execute('PRAGMA user_version=1')


def legacy_state(tmp_path):
    repository = SQLiteInventoryRepository(Database(tmp_path / 'source.sqlite3'))
    snapshot = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')
    ReconciliationService(repository).reconcile(snapshot)
    return repository.load()


def test_schema_one_migration_preserves_inventory_and_other_state(tmp_path):
    state = legacy_state(tmp_path)
    path = tmp_path / 'legacy.sqlite3'
    legacy_database(path, state.model_dump_json())
    for _ in range(2):
        database = Database(path)
        assert SQLiteInventoryRepository(database).load() == state
        with database.read() as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
            assert connection.execute("SELECT 1 FROM metadata WHERE id='reconciliation_inventory_v1'").fetchone() is None
            for table in ('presets', 'tags', 'jobs', 'metadata'):
                assert connection.execute(f"SELECT payload FROM {table} WHERE id='untouched'").fetchone()[0] == '{"keep": true}'
            assert connection.execute("SELECT width,scan_type FROM streams WHERE kind='video'").fetchone()[0] == 1920


@pytest.mark.parametrize('damage', ['malformed', 'missing_revision', 'mismatched_path', 'missing_file'])
def test_invalid_legacy_inventory_rolls_back_schema_and_retains_original(tmp_path, damage):
    state = legacy_state(tmp_path).model_dump(mode='json')
    if damage == 'missing_revision':
        state['revisions'] = {}
    elif damage == 'mismatched_path':
        next(iter(state['files'].values()))['relative_path'] = 'different.mkv'
    elif damage == 'missing_file':
        state['files'] = {}
    payload = 'not-json' if damage == 'malformed' else json.dumps(state)
    path = tmp_path / 'legacy.sqlite3'
    legacy_database(path, payload)
    with pytest.raises(RuntimeError, match='database unchanged'):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
        assert connection.execute("SELECT payload FROM metadata WHERE id='reconciliation_inventory_v1'").fetchone()[0] == payload
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='library_files'").fetchone() is None


def test_scale_indexed_projection_reconciliation_and_missing(tmp_path, monkeypatch):
    root = tmp_path / 'shows'
    processor = build_media_processor(Settings(media_backend='filesystem', shows_root=root,
                                              database_path=tmp_path / 'scale.sqlite3'))
    assert processor.inventory is not None
    service = processor.inventory
    repository = service.repository
    assert isinstance(repository, SQLiteInventoryRepository)
    root_id = root_identity('show', root)
    probe = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json').files[0].probe
    observations = [FileObservation(root_id=root_id,
                      relative_path=f"Series {show:03}/Series.{'S02' if show == 0 and ep == 100 else 'S01'}E{ep:03}.mkv",
                      media_id=f'episode-{show}-{ep}', scope='show', filesystem_id='fixture', inode=show * 1000 + ep,
                      size=2_000_000_000, mtime_ns=1, hardlinks=1, probe=probe)
                    for show in range(200) for ep in range(1, 101 if show == 0 else 21)]
    started = monotonic()
    result = service.reconcile(ScanSnapshot(root_id=root_id, sequence=1, status='complete', files=observations))
    assert len(result.created) == 4080
    # Runtime must never use the diagnostic full export.
    monkeypatch.setattr(repository, 'load', lambda: pytest.fail('full inventory export in runtime'))
    statements = []
    connect = repository.database._connect
    def traced():
        connection = connect()
        connection.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith('SELECT') else None)
        return connection
    monkeypatch.setattr(repository.database, '_connect', traced)
    assert processor.get_summary()['episodes'] == 4080
    cards = processor.get_shows()
    assert len(cards) == 200 and cards[0]['count'] == 100
    aggregate_queries = len(statements)
    assert aggregate_queries <= 8
    monkeypatch.setattr(processor.catalog.media, 'get_library',
                        lambda: pytest.fail('full media library loaded for show page'))
    with TestClient(create_app(processor=processor, start_workers=False)) as client:
        with monkeypatch.context() as restricted:
            for model in (FileObservation, MediaProbeResult):
                restricted.setattr(model, 'model_validate_json',
                                   lambda *args, **kwargs: pytest.fail('full probe JSON deserialized on show page'))
            statements.clear()
            started_list = monotonic()
            assert client.get('/shows').status_code == 200
            list_time = monotonic() - started_list
            list_queries = len(statements)
            assert list_queries <= 8
            statements.clear()
            started_detail = monotonic()
            assert client.get(f"/shows/{cards[0]['show'].id}").status_code == 200
            detail_time = monotonic() - started_detail
            detail_queries = len(statements)
            assert detail_queries <= 16
        statements.clear()
        with monkeypatch.context() as restricted:
            for method in ('files', 'load'):
                restricted.setattr(repository, method, lambda *args, **kwargs: pytest.fail('full file projection in tag lookup'))
            restricted.setattr(processor.catalog.media, 'select_library',
                               lambda **kwargs: pytest.fail('episode projection in tag lookup'))
            tagger = processor.catalog.tagger
            assert tagger is not None
            show_id = cards[0]['show'].id
            assert tagger.describe(TagTarget(kind='show', id=show_id))['target']['id'] == show_id
            assert tagger.describe(TagTarget(kind='season', id=show_id, season=1))['target']['season'] == 1
            assert tagger.describe(TagTarget(kind='season', id=show_id, season=2))['target']['season'] == 2
        assert len(statements) <= 9
    print(f'\nHTTP shows={list_time:.3f}s/{list_queries} SELECTs; '
          f'show detail={detail_time:.3f}s/{detail_queries} SELECTs')
    statements.clear()
    detail = processor.get_show(cards[0]['show'].id)
    assert len(detail['rows']) == 100
    page_queries = len(statements)
    assert page_queries <= 16  # independent of number of episodes, includes batched tags/presets
    statements.clear()
    second = service.reconcile(ScanSnapshot(root_id=root_id, sequence=2, status='complete', files=observations[:-1]))
    assert len(second.unchanged) == 4079 and len(second.missing) == 1
    assert not second.created and not second.revised
    assert processor.get_summary()['episodes'] == 4079
    print(f'\nScale: 4080 episodes / 200 shows; aggregate SELECTs={aggregate_queries}; '
          f'100-episode detail SELECTs={page_queries}; elapsed={monotonic()-started:.2f}s')


def test_artifact_and_historical_revision_survive_migration(tmp_path):
    from app.models.inventory import OutputArtifact
    from app.repositories.preset_seed import SeedPresetRepository
    repository = SQLiteInventoryRepository(Database(tmp_path / 'source.sqlite3'))
    service = ReconciliationService(repository)
    snapshot = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')
    file_id = service.reconcile(snapshot).created[0]
    reference = service.capture(file_id)
    preset = next(p for p in SeedPresetRepository(PROJECT_ROOT / 'fixtures/presets.json').get_all() if p.scope == 'movie')
    artifact = OutputArtifact(artifact_id='artifact', job_id='job', source=reference, root_id='workspace',
                              relative_path='test.mkv', preset=preset, status='simulated')
    repository.store_artifact(artifact)
    changed = snapshot.files[0].model_copy(update={'size': snapshot.files[0].size + 1, 'probe': None})
    service.reconcile(snapshot.model_copy(update={'sequence': 2, 'files': [changed]}))
    state = repository.load()
    assert len(state.revisions) == 2
    path = tmp_path / 'legacy.sqlite3'
    legacy_database(path, state.model_dump_json())
    migrated = SQLiteInventoryRepository(Database(path))
    assert migrated.load() == state
    restored = migrated.get_artifact('artifact')
    assert restored is not None and restored.source == reference


def test_current_revision_must_belong_to_file_in_database(tmp_path):
    repository = SQLiteInventoryRepository(Database(tmp_path / 'source.sqlite3'))
    service = ReconciliationService(repository)
    snapshot = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')
    second = snapshot.files[0].model_copy(update={'relative_path': 'second.mkv', 'media_id': 'second-media', 'inode': 555})
    result = service.reconcile(snapshot.model_copy(update={'files': [*snapshot.files, second]}))
    first, other = [repository.get_file(key) for key in result.created]
    assert first is not None and other is not None
    with pytest.raises(sqlite3.IntegrityError):
        with repository.transaction():
            repository.store_file(first.model_copy(update={'revision_id': other.revision_id}))
    restored = repository.get_file(first.file_id)
    assert restored is not None
    assert restored.revision_id == first.revision_id
