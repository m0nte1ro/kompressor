"""Transactional reconciliation of fixture observations, not a filesystem scanner."""
from uuid import uuid4
from collections import defaultdict

from app.models.inventory import (
    FileObservation, FileRevision, InventoryState, LibraryFile,
    ReconciliationIssue, ReconciliationResult, ScanSnapshot, SourceReference,
)
from app.repositories.inventory import InventoryRepository
from app.services.errors import Conflict, NotFound
from app.services.fingerprints import full_match, sample_match, unchanged


def new_id() -> str:
    return str(uuid4())


class ReconciliationService:
    def __init__(self, repository: InventoryRepository):
        self.repository = repository

    def capture(self, file_id: str) -> SourceReference:
        record = self.repository.get_file(file_id)
        if record is None:
            raise NotFound("Library file not found.")
        if record.presence != "present":
            raise Conflict("Library file is missing.")
        return SourceReference(file_id=file_id, revision_id=record.revision_id)

    def reconcile(self, snapshot: ScanSnapshot) -> ReconciliationResult:
        with self.repository.transaction():
            state = self.repository.root_state(snapshot.root_id, snapshot.files if snapshot.status != "complete" else None)
            if snapshot.sequence <= state.scan_sequences.get(snapshot.root_id, 0):
                raise Conflict("Stale or already applied scan sequence.")
            result = self._apply(state, snapshot)
            state.scan_sequences[snapshot.root_id] = snapshot.sequence
            self.repository.apply(state, snapshot, result)
            return result

    def _apply(self, state: InventoryState, snapshot: ScanSnapshot) -> ReconciliationResult:
        result = ReconciliationResult()
        records = [r.model_copy(deep=True) for r in state.files.values() if r.root_id == snapshot.root_id]
        observations = {item.relative_path: item for item in snapshot.files}
        claimed: set[str] = set()
        unresolved: set[str] = set()
        artifact_paths = {a.relative_path for a in state.artifacts.values() if a.root_id == snapshot.root_id}
        # Reserve unambiguous unchanged paths first; a second hardlink/copy must
        # not steal the original file record merely because it sorts first.
        stable = {r.relative_path: r for r in sorted(records, key=lambda r: r.presence == "present")
                  if r.relative_path in observations
                  and unchanged(r.observation, observations[r.relative_path])}
        by_media = defaultdict(list)
        by_path = defaultdict(list)
        for record in records:
            by_media[(record.media_id, record.scope)].append(record)
            by_path[record.relative_path].append(record)
        def rank(item: FileObservation) -> int:
            if item.relative_path in stable:
                return 0
            if snapshot.status == "complete" and any(
                self._same_media(r, item) and unchanged(r.observation, item)
                and (r.relative_path not in observations or
                     not unchanged(r.observation, observations[r.relative_path]))
                for r in by_media[(item.media_id, item.scope)]
            ):
                return 1
            return 2

        ordered = sorted(snapshot.files, key=rank)
        for item in ordered:
            if item.relative_path in artifact_paths:
                result.ignored_artifacts.append(item.relative_path)
                continue
            same_path = next((r for r in sorted(by_path[item.relative_path], key=lambda r: (r.presence == "missing", -r.last_seen_sequence))
                              if r.relative_path == item.relative_path and r.file_id not in claimed), None)
            record = stable.get(item.relative_path)
            # Renames/swaps only use records displaced from their previous path.
            candidates = [r for r in by_media[(item.media_id, item.scope)] if r.file_id not in claimed and self._same_media(r, item)
                          and (r.relative_path not in observations or
                               not unchanged(r.observation, observations[r.relative_path]))]
            physical = [r for r in candidates if item.physical_key() is not None
                        and r.observation.physical_key() == item.physical_key()
                        and unchanged(r.observation, item)]
            if record is None and snapshot.status == "complete" and len(physical) == 1:
                record = physical[0]
            if record is None and same_path is not None:
                # A replacement at the same library path keeps file_id. Semantic
                # reassignment is explicit: never transfer another movie's tags.
                record = same_path
            if record is None:
                plausible = [r for r in candidates if r.observation.size == item.size
                             and full_match(r.observation, item) is not False
                             and sample_match(r.observation, item) is not False]
                confirmed = [r for r in plausible if full_match(r.observation, item) is True]
                if snapshot.status == "complete" and len(plausible) == len(confirmed) == 1:
                    record = confirmed[0]
                elif plausible:
                    tier = "full" if any(sample_match(r.observation, item) is True or
                                         r.observation.fingerprints.full or item.fingerprints.full
                                         for r in plausible) else "sample"
                    result.issues.append(ReconciliationIssue(
                        path=item.relative_path, fingerprint_needed=tier,
                        candidate_file_ids=[r.file_id for r in plausible],
                        reason="Ambiguous relocation; complete scan and unique strong evidence required.",
                    ))
                    unresolved.update(r.file_id for r in plausible)
                    continue
            if record is not None and not self._same_media(record, item):
                result.issues.append(ReconciliationIssue(path=item.relative_path,
                                                         reason="Media reassignment requires explicit confirmation."))
                unresolved.add(record.file_id)
                continue
            if record is None:
                file_id, revision_id = new_id(), new_id()
                record = LibraryFile(file_id=file_id, revision_id=revision_id,
                                     media_id=item.media_id, scope=item.scope, root_id=item.root_id,
                                     relative_path=item.relative_path, observation=item,
                                     last_seen_sequence=snapshot.sequence)
                state.revisions[revision_id] = FileRevision(revision_id=revision_id, file_id=file_id, observation=item)
                result.created.append(file_id)
            else:
                if not unchanged(record.observation, item):
                    record.revision_id = new_id()
                    state.revisions[record.revision_id] = FileRevision(
                        revision_id=record.revision_id, file_id=record.file_id, observation=item)
                    result.revised.append(record.file_id)
                elif record.relative_path == item.relative_path:
                    result.unchanged.append(record.file_id)
                if record.relative_path != item.relative_path:
                    result.moved.append(record.file_id)
                # Do not discard stronger evidence merely because this cheap scan
                # didn't request hashing. Never carry old hashes to a new revision.
                if record.file_id not in result.revised:
                    item = self._retain_evidence(record.observation, item)
                record.relative_path = item.relative_path
                record.observation = item
                record.presence = "present"
                record.last_seen_sequence = snapshot.sequence
            state.files[record.file_id] = record
            claimed.add(record.file_id)
        if snapshot.status == "complete":
            protected = claimed | unresolved
            for record in records:
                if record.file_id not in protected and record.presence != "missing":
                    state.files[record.file_id].presence = "missing"
                    result.missing.append(record.file_id)
        return result

    @staticmethod
    def _same_media(record: LibraryFile, item: FileObservation) -> bool:
        return record.media_id == item.media_id and record.scope == item.scope

    @staticmethod
    def _retain_evidence(old: FileObservation, new: FileObservation) -> FileObservation:
        supplied = new.fingerprints
        previous = old.fingerprints
        merged = supplied.model_copy(update={
            "sample": supplied.sample or previous.sample,
            "sample_scheme": supplied.sample_scheme or previous.sample_scheme,
            "full": supplied.full or previous.full,
            "full_scheme": supplied.full_scheme or previous.full_scheme,
        })
        return new.model_copy(update={"fingerprints": merged, "probe": new.probe or old.probe})
