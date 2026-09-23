"""Fresh source checks at both execution boundaries. No replacement is performed."""
from typing import Protocol

from app.models.inventory import FileObservation, SourceReference
from app.repositories.inventory import InventoryRepository
from app.services.errors import Conflict, NotFound
from app.services.fingerprints import sample_match, full_match


class ObservationSource(Protocol):
    def observe(self, root_id: str, relative_path: str) -> FileObservation | None: ...


class SourceGuard:
    def __init__(self, repository: InventoryRepository, source: ObservationSource):
        self.repository = repository
        self.source = source

    def before_processing(self, reference: SourceReference) -> None:
        self._revalidate(reference)

    def before_replacement(self, reference: SourceReference) -> None:
        # Always observe again; a successful pre-processing check is not a permit.
        self._revalidate(reference)

    def _revalidate(self, reference: SourceReference) -> None:
        record = self.repository.get_file(reference.file_id)
        if record is None:
            raise NotFound("Library file not found.")
        if record.presence != "present" or record.revision_id != reference.revision_id:
            raise Conflict("Source is missing or its content revision changed.")
        if (reference.root_id is not None and record.root_id != reference.root_id
                or reference.relative_path is not None and record.relative_path != reference.relative_path):
            raise Conflict("Source root or relative path changed after the job was queued.")
        old = record.observation
        if reference.captured and (
                old.filesystem_id != reference.filesystem_id
                or old.inode != reference.inode
                or old.generation != reference.generation
                or old.size != reference.size
                or old.mtime_ns != reference.mtime_ns
                or old.ctime_ns != reference.ctime_ns
                or old.hardlinks != reference.hardlinks):
            raise Conflict("Source physical identity or stat facts changed after the job was queued.")
        fresh = self.source.observe(record.root_id, record.relative_path)
        if fresh is None:
            raise Conflict("Source cannot be revalidated: file/root unavailable.")
        if fresh.hardlinks is None or fresh.hardlinks != 1:
            raise Conflict("Source is hardlinked or its hardlink count is unknown.")
        # Deliberately stricter than reconciliation: even a byte-identical physical
        # replacement needs a new review. Hash equality cannot bypass these checks.
        if (fresh.root_id != old.root_id or fresh.relative_path != old.relative_path
                or fresh.media_id != old.media_id or fresh.scope != old.scope
                or fresh.physical_key() is None or fresh.physical_key() != old.physical_key()
                or fresh.size != old.size or fresh.mtime_ns != old.mtime_ns
                or fresh.ctime_ns != old.ctime_ns
                or full_match(old, fresh) is False or sample_match(old, fresh) is False):
            raise Conflict("Source changed since its revision was captured. Reconcile and review again.")
