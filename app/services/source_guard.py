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
        record = self.repository.load().files.get(reference.file_id)
        if record is None:
            raise NotFound("Library file not found.")
        if record.presence != "present" or record.revision_id != reference.revision_id:
            raise Conflict("Source is missing or its content revision changed.")
        fresh = self.source.observe(record.root_id, record.relative_path)
        if fresh is None:
            raise Conflict("Source cannot be revalidated: file/root unavailable.")
        old = record.observation
        if fresh.hardlinks is None or fresh.hardlinks != 1:
            raise Conflict("Source is hardlinked or its hardlink count is unknown.")
        # Deliberately stricter than reconciliation: even a byte-identical physical
        # replacement needs a new review. Hash equality cannot bypass these checks.
        if (fresh.root_id != old.root_id or fresh.relative_path != old.relative_path
                or fresh.media_id != old.media_id or fresh.scope != old.scope
                or fresh.physical_key() is None or fresh.physical_key() != old.physical_key()
                or fresh.size != old.size or fresh.mtime_ns != old.mtime_ns
                or full_match(old, fresh) is False or sample_match(old, fresh) is False):
            raise Conflict("Source changed since its revision was captured. Reconcile and review again.")
