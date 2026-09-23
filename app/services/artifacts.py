"""Fixture lifecycle for independent test outputs; never mutates library sources."""
from typing import Literal

from app.models.inventory import FileObservation, OutputArtifact, SourceReference
from app.models.preset import CompressionPreset
from app.repositories.inventory import InventoryRepository
from app.services.errors import Conflict, InvalidOperation, NotFound
from app.services.reconciliation import new_id
from app.services.source_guard import SourceGuard


class ArtifactService:
    def __init__(self, repository: InventoryRepository, guard: SourceGuard):
        self.repository = repository
        self.guard = guard

    def plan(self, job_id: str, source: SourceReference, preset: CompressionPreset,
             root_id: str, relative_path: str,
             mode: Literal["keep_output", "replace_source"] = "keep_output") -> OutputArtifact:
        # Use the same lexical location validation as library observations.
        relative_path = FileObservation.safe_relative_path(relative_path)
        if not root_id or not job_id:
            raise InvalidOperation("Artifact root and job ID are required.")
        with self.repository.transaction():
            file = self.repository.get_file(source.file_id)
            if file is None:
                raise NotFound("Library file not found.")
            if source.revision_id != file.revision_id or file.presence != "present":
                raise Conflict("Source revision is no longer current.")
            if preset.scope != file.scope:
                raise Conflict("Preset scope does not match source media.")
            if self.repository.artifacts(job_id=job_id):
                raise Conflict("A test artifact already exists for this job.")
            if self.repository.files(root_id=root_id, path=relative_path):
                raise Conflict("Output artifact cannot occupy a library-file location.")
            if any((a.root_id, a.relative_path) == (root_id, relative_path) for a in self.repository.artifacts(root_id=root_id)):
                raise Conflict("Output artifact location is already reserved.")
            artifact = OutputArtifact(artifact_id=new_id(), job_id=job_id, source=source,
                                      preset=preset.model_copy(deep=True), root_id=root_id,
                                      relative_path=relative_path, mode=mode)
            self.repository.store_artifact(artifact)
            return artifact

    def simulate(self, artifact_id: str) -> OutputArtifact:
        # This is a fixture exercise, not an alternative real encoder/queue.
        with self.repository.transaction():
            artifact = self.repository.get_artifact(artifact_id)
            if artifact is None:
                raise NotFound("Output artifact not found.")
            if artifact.status != "planned":
                raise Conflict("Artifact has already been simulated.")
            self.guard.before_processing(artifact.source)
            artifact.status = "simulated"
            self.repository.store_artifact(artifact)
            return artifact

    def before_replacement(self, artifact_id: str) -> None:
        artifact = self.repository.get_artifact(artifact_id)
        if artifact is None:
            raise NotFound("Output artifact not found.")
        self.guard.before_replacement(artifact.source)
        if artifact.mode != "replace_source":
            raise Conflict("Keep-output artifacts cannot replace a source.")
        # Even a successful source check is insufficient: no real output has been
        # produced/validated. Never mark the source processed or replace it here.
        raise InvalidOperation("Replacement is unavailable for fixture artifacts.")
