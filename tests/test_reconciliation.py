from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT
from app.models.inventory import FileObservation, ScanSnapshot
from app.repositories.database import Database
from app.repositories.discovery_fixture import read_snapshot
from app.repositories.inventory import SQLiteInventoryRepository
from app.services.errors import Conflict
from app.services.reconciliation import ReconciliationService


@pytest.fixture
def snapshot() -> ScanSnapshot:
    return read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')


@pytest.fixture
def inventory(tmp_path: Path) -> ReconciliationService:
    return ReconciliationService(SQLiteInventoryRepository(Database(tmp_path / 'inventory.sqlite3')))


def changed(item: FileObservation, **updates) -> FileObservation:
    return FileObservation.model_validate({**item.model_dump(), **updates})


def scan(snapshot: ScanSnapshot, sequence: int, files: list[FileObservation], status: str = 'complete') -> ScanSnapshot:
    return ScanSnapshot.model_validate(dict(root_id=snapshot.root_id, sequence=sequence, files=files, status=status))


def test_initial_rescan_restart_and_probe_facts(inventory, snapshot):
    first = inventory.reconcile(snapshot)
    file_id = first.created[0]
    before = inventory.repository.load()
    reference = inventory.capture(file_id)
    second = inventory.reconcile(scan(snapshot, 2, snapshot.files))
    assert second.unchanged == [file_id] and not second.created
    assert inventory.capture(file_id) == reference
    assert len(inventory.repository.load().revisions) == 1
    # Reload the actual persisted document rather than sharing object references.
    reloaded = inventory.repository.load()
    assert reloaded.files[file_id].media_id == 'movie-king-of-comedy'
    probe = reloaded.files[file_id].observation.probe
    assert probe is not None
    assert len([s for s in probe.streams if s.kind == 'audio']) == 2
    assert len([s for s in probe.streams if s.kind == 'subtitle']) == 2
    assert probe.streams[0].resolution_class == 1080
    assert probe.streams[0].scan_type == 'interlaced'
    assert probe.streams[1].channel_layout == '5.1'
    assert probe.streams[3].dispositions['forced']
    assert probe.chapters[0].title == 'Opening'
    assert reloaded.revisions == before.revisions


def test_rename_keeps_logical_file_revision_and_media(inventory, snapshot):
    file_id = inventory.reconcile(snapshot).created[0]
    reference = inventory.capture(file_id)
    moved = changed(snapshot.files[0], relative_path='Comedy/new-name.mkv')
    result = inventory.reconcile(scan(snapshot, 2, [moved]))
    assert result.moved == [file_id]
    assert not result.created and not result.revised
    assert inventory.capture(file_id) == reference
    assert inventory.repository.load().files[file_id].relative_path == moved.relative_path


@pytest.mark.parametrize('changes', [
    {'size': 123}, {'mtime_ns': 999}, {'inode': 808}, {'generation': 'inode-reused'},
])
def test_replacement_keeps_file_id_but_invalidates_revision_probe(inventory, snapshot, changes):
    file_id = inventory.reconcile(snapshot).created[0]
    reference = inventory.capture(file_id)
    replacement = changed(snapshot.files[0], probe=None, **changes)
    result = inventory.reconcile(scan(snapshot, 2, [replacement]))
    assert result.revised == [file_id] and not result.created
    state = inventory.repository.load()
    assert state.files[file_id].revision_id != reference.revision_id
    assert state.files[file_id].observation.probe is None
    assert state.revisions[reference.revision_id].observation.probe is not None
    assert len(state.revisions) == 2


@pytest.mark.parametrize('status', ['partial', 'unavailable'])
def test_incomplete_or_unavailable_root_does_not_remove_files(inventory, snapshot, status):
    file_id = inventory.reconcile(snapshot).created[0]
    result = inventory.reconcile(scan(snapshot, 2, [], status))
    assert not result.missing
    assert inventory.repository.load().files[file_id].presence == 'present'


def test_complete_scan_marks_missing_and_return_keeps_identity(inventory, snapshot):
    file_id = inventory.reconcile(snapshot).created[0]
    reference = inventory.capture(file_id)
    assert inventory.reconcile(scan(snapshot, 2, [])).missing == [file_id]
    with pytest.raises(Conflict):
        inventory.capture(file_id)
    inventory.reconcile(scan(snapshot, 3, snapshot.files))
    assert inventory.capture(file_id) == reference


def test_failed_other_root_does_not_affect_existing_root(inventory, snapshot):
    file_id = inventory.reconcile(snapshot).created[0]
    inventory.reconcile(ScanSnapshot(root_id='shows-fixture', sequence=1, status='complete'))
    assert inventory.repository.load().files[file_id].presence == 'present'


@pytest.mark.parametrize('hardlinks', [1, 2])
def test_copy_or_second_hardlink_is_separate_record(inventory, snapshot, hardlinks):
    original = changed(snapshot.files[0], hardlinks=hardlinks)
    file_id = inventory.reconcile(scan(snapshot, 1, [original])).created[0]
    copied = changed(original, relative_path='AAA-copy.mkv', inode=101 if hardlinks == 2 else 202)
    result = inventory.reconcile(scan(snapshot, 2, [copied, original]))
    assert len(result.created) == 1 and not result.moved
    assert result.created[0] != file_id
    assert len(inventory.repository.load().files) == 2


def test_cross_device_move_uses_full_hash_only_on_ambiguous_candidates(inventory, snapshot):
    hashes = {'sample_scheme': 'sample-v1', 'sample': 'sample-a', 'full_scheme': 'sha256', 'full': 'full-a'}
    old = changed(snapshot.files[0], fingerprints=hashes)
    file_id = inventory.reconcile(scan(snapshot, 1, [old])).created[0]
    reference = inventory.capture(file_id)
    moved = changed(old, relative_path='Elsewhere/movie.mkv', filesystem_id='fixture-fs-b', inode=77, mtime_ns=200)
    result = inventory.reconcile(scan(snapshot, 2, [moved]))
    assert result.moved == [file_id] and not result.revised
    assert inventory.capture(file_id) == reference


def test_partial_fingerprint_only_requests_stronger_evidence(inventory, snapshot):
    hashes = {'sample_scheme': 'sample-v1', 'sample': 'same-sample'}
    old = changed(snapshot.files[0], fingerprints=hashes)
    file_id = inventory.reconcile(scan(snapshot, 1, [old])).created[0]
    moved = changed(old, relative_path='candidate.mkv', inode=999)
    result = inventory.reconcile(scan(snapshot, 2, [moved]))
    assert not result.created and not result.moved and not result.missing
    assert result.issues[0].fingerprint_needed == 'full'
    assert inventory.capture(file_id)


def test_cheap_ambiguous_candidate_requests_sample_not_full_hash(inventory, snapshot):
    inventory.reconcile(snapshot)
    moved = changed(snapshot.files[0], relative_path='candidate.mkv', inode=999)
    result = inventory.reconcile(scan(snapshot, 2, [moved]))
    assert result.issues[0].fingerprint_needed == 'sample'
    assert not result.created


def test_same_sample_cannot_hide_content_change(inventory, snapshot):
    hashes = {'sample_scheme': 'sample-v1', 'sample': 'same-sample'}
    old = changed(snapshot.files[0], fingerprints=hashes)
    file_id = inventory.reconcile(scan(snapshot, 1, [old])).created[0]
    result = inventory.reconcile(scan(snapshot, 2, [changed(old, mtime_ns=500)]))
    assert result.revised == [file_id]


def test_hash_mismatch_beats_unchanged_stat(inventory, snapshot):
    old = changed(snapshot.files[0], fingerprints={'full_scheme': 'sha256', 'full': 'one'})
    file_id = inventory.reconcile(scan(snapshot, 1, [old])).created[0]
    result = inventory.reconcile(scan(snapshot, 2, [changed(old, fingerprints={'full_scheme': 'sha256', 'full': 'two'})]))
    assert result.revised == [file_id]


def test_strong_evidence_survives_cheap_rescan(inventory, snapshot):
    old = changed(snapshot.files[0], fingerprints={'full_scheme': 'sha256', 'full': 'one'})
    file_id = inventory.reconcile(scan(snapshot, 1, [old])).created[0]
    inventory.reconcile(scan(snapshot, 2, snapshot.files))
    assert inventory.repository.load().files[file_id].observation.fingerprints.full == 'one'


def test_multiple_identical_missing_candidates_are_not_arbitrarily_merged(inventory, snapshot):
    old = changed(snapshot.files[0], fingerprints={'full_scheme': 'sha256', 'full': 'same'})
    other = changed(old, relative_path='other.mkv', inode=222)
    inventory.reconcile(scan(snapshot, 1, [old, other]))
    result = inventory.reconcile(scan(snapshot, 2, [changed(old, relative_path='new.mkv', inode=333)]))
    assert result.issues and not result.moved and not result.created
    assert len(inventory.repository.load().files) == 2


def test_swap_paths_uses_identity_not_scan_order(inventory, snapshot):
    old = snapshot.files[0]
    other = changed(old, relative_path='other.mkv', inode=222, size=123)
    ids = inventory.reconcile(scan(snapshot, 1, [old, other])).created
    result = inventory.reconcile(scan(snapshot, 2, [changed(other, relative_path=old.relative_path),
                                                   changed(old, relative_path=other.relative_path)]))
    assert set(result.moved) == set(ids)
    assert not result.revised and not result.created


def test_semantic_reassignment_does_not_transfer_identity(inventory, snapshot):
    file_id = inventory.reconcile(snapshot).created[0]
    result = inventory.reconcile(scan(snapshot, 2, [changed(snapshot.files[0], media_id='different-movie')]))
    assert result.issues and not result.created
    assert inventory.repository.load().files[file_id].media_id == 'movie-king-of-comedy'


def test_stale_scan_rejected_without_state_change(inventory, snapshot):
    inventory.reconcile(snapshot)
    before = inventory.repository.load()
    with pytest.raises(Conflict, match='sequence'):
        inventory.reconcile(snapshot)
    assert inventory.repository.load() == before


def test_atomic_reconciliation_rollback(inventory, snapshot, monkeypatch):
    save = inventory.repository.apply
    def fail_after_save(*args):
        save(*args)
        raise RuntimeError('storage failure')
    monkeypatch.setattr(inventory.repository, 'apply', fail_after_save)
    with pytest.raises(RuntimeError):
        inventory.reconcile(snapshot)
    assert not inventory.repository.load().files
    assert not inventory.repository.load().scan_sequences


@pytest.mark.parametrize('path', ['../movie.mkv', '/media/movie.mkv', '.', ''])
def test_observations_reject_paths_outside_root(snapshot, path):
    with pytest.raises(ValidationError):
        changed(snapshot.files[0], relative_path=path)


def test_duplicate_observations_rejected(snapshot):
    with pytest.raises(ValidationError):
        scan(snapshot, 2, [*snapshot.files, *snapshot.files])


def test_facade_reconciliation_is_persistent_and_does_not_change_seed_catalog(runtime, snapshot):
    app, client = runtime
    processor = app.state.media_processor
    before = client.get('/api/library').json()
    created = processor.reconcile_library(snapshot).created
    assert len(created) == 1
    assert processor.inventory.repository.load().files[created[0]].media_id == 'movie-king-of-comedy'
    assert client.get('/api/library').json() == before


@pytest.mark.parametrize('reverse', [False, True])
def test_rename_plus_replacement_is_independent_of_scan_order(inventory, snapshot, reverse):
    file_id = inventory.reconcile(snapshot).created[0]
    reference = inventory.capture(file_id)
    old = snapshot.files[0]
    moved = changed(old, relative_path='moved-original.mkv')
    replaced = changed(old, inode=333, size=456, probe=None)
    files = [replaced, moved] if not reverse else [moved, replaced]
    result = inventory.reconcile(scan(snapshot, 2, files))
    assert result.moved == [file_id] and len(result.created) == 1
    assert not result.revised
    assert inventory.capture(file_id) == reference
    assert inventory.repository.load().files[file_id].relative_path == moved.relative_path
