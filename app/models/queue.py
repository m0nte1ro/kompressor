from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.media import MediaScope
from app.models.preset import CompressionPreset, EncoderBackend
from app.models.inventory import SourceReference


JobStatus = Literal["queued", "encoding", "validating", "replacing", "stopping",
                    "completed", "skipped", "blocked", "failed"]
Priority = Literal["low", "normal", "high", "urgent"]
# "replacing" is active but never stoppable: the source swap must run to a known state.
ACTIVE_STATUSES = frozenset({"encoding", "validating", "replacing", "stopping"})
# Not finished yet. Repositories index on this, so polling never reads history.
PENDING_STATUSES = frozenset({"queued", *ACTIVE_STATUSES})
# Every status change a job may make (QueueJob.move_to); anything else is a bug.
# completed, skipped, blocked and failed are final.
JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    # Claimed by its lane, or refused by a policy or mode check.
    "queued": frozenset({"encoding", "blocked"}),
    # Stopped on request, requeued after a restart or storage outage, blocked by
    # a policy change, failed, or skipped once its stop is acknowledged.
    "encoding": frozenset({"validating", "stopping", "queued", "skipped", "blocked", "failed"}),
    # Kept (completed), swapped in (replacing), or skipped below the minimum saving.
    "validating": frozenset({"completed", "replacing", "skipped", "stopping", "queued",
                             "blocked", "failed"}),
    # A swap is never stopped, requeued or blocked: it is replaced or kept, or it fails.
    "replacing": frozenset({"completed", "failed"}),
    "stopping": frozenset({"skipped", "queued", "blocked", "failed"}),
    "completed": frozenset(), "skipped": frozenset(), "blocked": frozenset(), "failed": frozenset(),
}
# Nominal video bitrate (bit/s) a user picks for a source whose size is outside
# the preset's per-resolution bitrate table.
ChosenVideoBitrate = Annotated[int, Field(ge=100_000, le=200_000_000)]
# preserve_subtitles_and_metadata keeps subtitle and attachment streams, chapters and
# container tags: everything but the audio and video. Jobs saved and API callers
# written before the rename say preserve_subtitles.
SUBTITLES_AND_METADATA = AliasChoices("preserve_subtitles_and_metadata", "preserve_subtitles")


class EnqueueRequest(BaseModel):
    # The client supplies a preset ID, never technical encoder overrides. The one
    # exception is video_bitrate, used only by sources outside the preset's bitrate table.
    model_config = ConfigDict(extra="forbid")
    media_ids: list[str] = Field(min_length=1, max_length=500)
    scope: MediaScope
    preset_id: str
    # None preserves audio. Conversion requires an explicit per-job False.
    preserve_audio: bool | None = None
    preserve_subtitles_and_metadata: bool = Field(default=True, validation_alias=SUBTITLES_AND_METADATA)
    # API callers keep the original unless they ask; the WebUI modal defaults to replace.
    replace_source: bool = False
    video_bitrate: ChosenVideoBitrate | None = None


class ReplacementJournal(BaseModel):
    """Persisted before every filesystem step so an interrupted swap can be resolved."""
    model_config = ConfigDict(extra="forbid")
    target: str
    incoming: str
    backup: str
    source_device: int
    source_inode: int
    output_size: int = Field(ge=0)
    incoming_device: int | None = None
    incoming_inode: int | None = None
    phase: Literal["copying", "copied", "swapping", "swapped", "verified"] = "copying"


class PriorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: Priority


class InvalidTransition(RuntimeError):
    """A status change outside JOB_TRANSITIONS."""


class QueueJob(BaseModel):
    id: str
    media_id: str
    scope: MediaScope
    name: str
    backend: EncoderBackend
    preset: CompressionPreset
    preserve_audio: bool
    requested_preserve_audio: bool | None = None
    preserve_subtitles_and_metadata: bool = Field(validation_alias=SUBTITLES_AND_METADATA)
    source_size: int
    estimated_output_size: int | None
    estimated_saving: int | None
    planning_output_size: int | None = None
    planning_saving: int | None = None
    planning_saving_percent: float | None = None
    source_codec: str
    execution_mode: Literal["fake", "real"] = "fake"
    source_file_id: str | None = None
    source_revision_id: str | None = None
    source_reference: SourceReference | None = None
    replace_source: bool = False
    # Set only when the source size is outside the preset's bitrate table.
    chosen_video_bitrate: ChosenVideoBitrate | None = None
    replacement: ReplacementJournal | None = None
    source_replaced: bool = False
    # History "Replace source": this job swaps in the kept output of replaces_job_id
    # instead of encoding again.
    reuse_output_path: str | None = None
    replaces_job_id: str | None = None
    # Latest History quality tools run on a kept output (see services/analysis.py).
    comparison: dict | None = None
    vmaf: dict | None = None
    estimate_basis: str = "bitrate"
    estimated_saving_low: int | None = None
    estimated_saving_high: int | None = None
    priority: Priority = "normal"
    move_next_order: int = 0
    status: JobStatus = "queued"
    progress: float = 0
    progress_known: bool = False
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0
    reasons: list[str] = Field(default_factory=list)
    output_path: str | None = None
    output_size: int | None = None
    measured_saving: int | None = None
    error_message: str | None = None
    ffmpeg_exit_code: int | None = None
    validation_errors: list[str] = Field(default_factory=list)
    cancel_requested: bool = False
    cancel_reason: str | None = None

    def move_to(self, status: JobStatus) -> None:
        """Change status along JOB_TRANSITIONS; staying in the same status is allowed."""
        if status != self.status and status not in JOB_TRANSITIONS[self.status]:
            raise InvalidTransition(f"A {self.status} job cannot become {status}.")
        self.status = status
