import json
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.models.inventory import FileObservation, ScanSnapshot
from app.models.probe import HDRSignalling
from app.repositories.database import Database
from app.repositories.discovery_fixture import FixtureObservationSource, read_snapshot
from app.repositories.inventory import SQLiteInventoryRepository
from app.repositories.preset_seed import SeedPresetRepository
from app.services.artifacts import ArtifactService
from app.services.errors import Conflict, InvalidOperation
from app.services.reconciliation import ReconciliationService
from app.services.source_guard import SourceGuard


@pytest.fixture
def scenario(tmp_path: Path):
    snapshot = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')
    repository = SQLiteInventoryRepository(Database(tmp_path / 'state.sqlite3'))
    service = ReconciliationService(repository)
    file_id = service.reconcile(snapshot).created[0]
    source = FixtureObservationSource()
    source.load_snapshot(snapshot)
    guard = SourceGuard(repository, source)
    return snapshot, service, source, guard, service.capture(file_id)


def replace_observation(snapshot: ScanSnapshot, **changes) -> ScanSnapshot:
    item = FileObservation.model_validate({**snapshot.files[0].model_dump(), **changes})
    return ScanSnapshot(root_id=snapshot.root_id, sequence=snapshot.sequence + 1, status='complete', files=[item])


@pytest.mark.parametrize('boundary', ['before_processing', 'before_replacement'])
@pytest.mark.parametrize('changes', [
    {'hardlinks': 2}, {'hardlinks': None}, {'size': 0}, {'mtime_ns': 44}, {'ctime_ns': 45},
    {'inode': 111}, {'generation': 'reused'}, {'filesystem_id': None},
])
def test_both_boundaries_require_fresh_revision_and_hardlinks(scenario, boundary, changes):
    snapshot, service, source, guard, reference = scenario
    guard.before_processing(reference)
    # Change the fixture source WITHOUT reconciling. Cached database state is stale.
    source.load_snapshot(replace_observation(snapshot, **changes))
    with pytest.raises(Conflict):
        getattr(guard, boundary)(reference)


@pytest.mark.parametrize('boundary', ['before_processing', 'before_replacement'])
def test_changed_persisted_revision_invalidates_captured_reference(scenario, boundary):
    snapshot, service, source, guard, reference = scenario
    changed = replace_observation(snapshot, size=1)
    source.load_snapshot(changed)
    service.reconcile(changed)
    with pytest.raises(Conflict, match='revision'):
        getattr(guard, boundary)(reference)


def test_missing_mount_between_processing_and_replacement_blocks(scenario):
    snapshot, service, source, guard, reference = scenario
    guard.before_processing(reference)
    source.load_snapshot(ScanSnapshot(root_id=snapshot.root_id, sequence=2, status='unavailable'))
    with pytest.raises(Conflict, match='unavailable'):
        guard.before_replacement(reference)


def test_each_boundary_observes_source_again(scenario, monkeypatch):
    snapshot, service, source, guard, reference = scenario
    calls = []
    observe = source.observe
    def counting_observe(root, path):
        calls.append((root, path))
        return observe(root, path)
    monkeypatch.setattr(source, 'observe', counting_observe)
    guard.before_processing(reference)
    guard.before_replacement(reference)
    assert len(calls) == 2


def test_artifact_identity_snapshot_and_simulation_do_not_mark_source_processed(scenario):
    snapshot, service, source, guard, reference = scenario
    artifacts = ArtifactService(service.repository, guard)
    preset = SeedPresetRepository(PROJECT_ROOT / 'fixtures/presets.json').get_all()[0]
    output = artifacts.plan('fixture-job', reference, preset, 'workspace-fixture', 'jobs/test/output.mkv')
    assert output.artifact_id not in (reference.file_id, reference.revision_id)
    assert output.mode == 'keep_output'
    preset.name = 'User edit after submission'
    assert service.repository.load().artifacts[output.artifact_id].preset.name != preset.name
    source_state = service.repository.load().files
    result = artifacts.simulate(output.artifact_id)
    assert result.status == 'simulated'
    assert service.repository.load().files == source_state
    assert service.capture(reference.file_id) == reference
    assert len(service.repository.load().revisions) == 1
    with pytest.raises(Conflict, match='Keep-output'):
        artifacts.before_replacement(output.artifact_id)


def test_replace_intent_cannot_replace_even_after_simulation(scenario):
    snapshot, service, source, guard, reference = scenario
    artifacts = ArtifactService(service.repository, guard)
    preset = SeedPresetRepository(PROJECT_ROOT / 'fixtures/presets.json').get_all()[0]
    output = artifacts.plan('fixture-job', reference, preset, 'workspace-fixture', 'output.mkv', 'replace_source')
    artifacts.simulate(output.artifact_id)
    with pytest.raises(InvalidOperation, match='unavailable'):
        artifacts.before_replacement(output.artifact_id)
    source.load_snapshot(replace_observation(snapshot, hardlinks=2))
    with pytest.raises(Conflict, match='hardlink'):
        artifacts.before_replacement(output.artifact_id)


def test_test_output_simulation_runs_processing_guard(scenario):
    snapshot, service, source, guard, reference = scenario
    artifacts = ArtifactService(service.repository, guard)
    preset = SeedPresetRepository(PROJECT_ROOT / 'fixtures/presets.json').get_all()[0]
    output = artifacts.plan('fixture-job', reference, preset, 'workspace-fixture', 'output.mkv')
    source.load_snapshot(replace_observation(snapshot, hardlinks=2))
    with pytest.raises(Conflict):
        artifacts.simulate(output.artifact_id)
    assert service.repository.load().artifacts[output.artifact_id].status == 'planned'


def test_artifact_locations_are_reserved_and_scans_do_not_import_outputs(scenario):
    snapshot, service, source, guard, reference = scenario
    artifacts = ArtifactService(service.repository, guard)
    preset = SeedPresetRepository(PROJECT_ROOT / 'fixtures/presets.json').get_all()[0]
    with pytest.raises(Conflict, match='library-file'):
        artifacts.plan('bad-job', reference, preset, snapshot.root_id, snapshot.files[0].relative_path)
    output = artifacts.plan('fixture-job', reference, preset, snapshot.root_id, 'test-output.mkv')
    with pytest.raises(Conflict):
        artifacts.plan('duplicate', reference, preset, snapshot.root_id, output.relative_path)
    observed_output = FileObservation.model_validate({**snapshot.files[0].model_dump(),
                                                     'relative_path': output.relative_path, 'inode': 222})
    result = service.reconcile(ScanSnapshot(root_id=snapshot.root_id, sequence=2, status='complete',
                                           files=[*snapshot.files, observed_output]))
    assert result.ignored_artifacts == [output.relative_path]
    assert not result.created and len(service.repository.load().files) == 1


def test_raw_hdr_facts_and_derived_classifications():
    fixtures = json.loads((PROJECT_ROOT / 'fixtures/reconciliation/hdr.json').read_text())
    facts = {name: HDRSignalling.model_validate(value) for name, value in fixtures.items()}
    assert facts['hdr10'].classify().base == 'hdr10'
    assert not facts['hdr10'].classify().uncertain
    assert facts['hdr10'].mastering_display == {'max_luminance': '1000/1'}
    mixed = facts['dv_plus'].classify()
    assert mixed.base == 'unknown' and mixed.dolby_vision and mixed.hdr10plus
    assert mixed.uncertain
    assert facts['incomplete'].classify().uncertain
    assert facts['hlg'].classify().base == 'hlg'
    assert HDRSignalling(bit_depth=10).classify().base == 'unknown'
    # Classification isn't stored as an independently editable/stale HDR label.
    assert 'base' not in facts['hdr10'].model_dump()


def test_fixture_probe_returns_technical_facts_without_media_identity(scenario):
    snapshot, service, source, guard, reference = scenario
    facts = source.probe(snapshot.root_id, snapshot.files[0].relative_path)
    assert 'media_id' not in facts.model_dump()
    facts.streams[0].codec = 'changed'
    assert source.probe(snapshot.root_id, snapshot.files[0].relative_path).streams[0].codec == 'h264'


def test_reconciliation_persists_after_new_repository_instance(tmp_path):
    path = tmp_path / 'state.sqlite3'
    snapshot = read_snapshot(PROJECT_ROOT / 'fixtures/reconciliation/initial.json')
    first = ReconciliationService(SQLiteInventoryRepository(Database(path)))
    file_id = first.reconcile(snapshot).created[0]
    reference = first.capture(file_id)
    second = ReconciliationService(SQLiteInventoryRepository(Database(path)))
    assert second.capture(file_id) == reference
    assert isinstance(second.repository, SQLiteInventoryRepository)
    assert second.repository.load().scan_sequences[snapshot.root_id] == 1
