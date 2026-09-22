"""Persistent logical files, immutable content revisions and separate artifacts."""
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.media import MediaScope
from app.models.preset import CompressionPreset
from app.models.probe import MediaProbeResult


class Fingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Digests are supplied by adapters/fixtures, never computed during reconciliation.
    # Scheme includes algorithm and sample layout, preventing incompatible comparisons.
    sample_scheme: str | None = None
    sample: str | None = None
    full_scheme: str | None = None
    full: str | None = None

    @model_validator(mode="after")
    def paired_digests(self):
        if bool(self.sample_scheme) != bool(self.sample) or bool(self.full_scheme) != bool(self.full):
            raise ValueError("Each fingerprint needs a scheme and nonempty digest.")
        return self


class FileObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root_id: str = Field(min_length=1)
    relative_path: str
    media_id: str = Field(min_length=1)
    scope: MediaScope
    filesystem_id: str | None = None
    inode: int | None = Field(default=None, ge=0)
    # Optional adapter-supplied inode generation/birth identity helps detect reuse.
    generation: str | None = None
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    hardlinks: int | None = Field(default=None, ge=1)
    fingerprints: Fingerprints = Field(default_factory=Fingerprints)
    probe: MediaProbeResult | None = None

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or str(path) == ".":
            raise ValueError("Expected a relative path inside the configured root.")
        return str(path)

    def physical_key(self) -> tuple[str, int, str | None] | None:
        if self.filesystem_id is None or self.inode is None:
            return None
        return self.filesystem_id, self.inode, self.generation


class ScanSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str = Field(min_length=1)
    # Monotonically increasing per root; incomplete scans consume a sequence too.
    sequence: int = Field(ge=1)
    status: Literal["complete", "partial", "unavailable"]
    files: list[FileObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_root(self):
        if any(item.root_id != self.root_id for item in self.files):
            raise ValueError("All observations must belong to the scanned root.")
        paths = [item.relative_path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate path in scan snapshot.")
        if self.status == "unavailable" and self.files:
            raise ValueError("Unavailable roots cannot contain observations.")
        return self


class FileRevision(BaseModel):
    model_config = ConfigDict(frozen=True)
    revision_id: str
    file_id: str
    observation: FileObservation


class LibraryFile(BaseModel):
    file_id: str
    media_id: str
    scope: MediaScope
    root_id: str
    relative_path: str
    revision_id: str
    observation: FileObservation
    presence: Literal["present", "missing"] = "present"
    last_seen_sequence: int


class SourceReference(BaseModel):
    model_config = ConfigDict(frozen=True)
    file_id: str
    revision_id: str


class OutputArtifact(BaseModel):
    artifact_id: str
    job_id: str
    source: SourceReference
    root_id: str
    relative_path: str
    preset: CompressionPreset
    mode: Literal["keep_output", "replace_source"] = "keep_output"
    status: Literal["planned", "simulated"] = "planned"
    # No simulated artifact can establish that a library revision was processed.


class InventoryState(BaseModel):
    files: dict[str, LibraryFile] = Field(default_factory=dict)
    revisions: dict[str, FileRevision] = Field(default_factory=dict)
    artifacts: dict[str, OutputArtifact] = Field(default_factory=dict)
    scan_sequences: dict[str, int] = Field(default_factory=dict)


class ReconciliationIssue(BaseModel):
    path: str
    reason: str
    fingerprint_needed: Literal["sample", "full"] | None = None
    candidate_file_ids: list[str] = Field(default_factory=list)


class ReconciliationResult(BaseModel):
    created: list[str] = Field(default_factory=list)
    revised: list[str] = Field(default_factory=list)
    moved: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    ignored_artifacts: list[str] = Field(default_factory=list)
    issues: list[ReconciliationIssue] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
