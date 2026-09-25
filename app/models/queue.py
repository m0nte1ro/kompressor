from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.media import MediaScope
from app.models.preset import CompressionPreset, EncoderBackend
from app.models.inventory import SourceReference


JobStatus = Literal["queued", "encoding", "validating", "stopping", "completed", "skipped", "blocked", "failed"]
Priority = Literal["low", "normal", "high", "urgent"]


class EnqueueRequest(BaseModel):
    # The client supplies a preset ID, never technical encoder overrides.
    model_config = ConfigDict(extra="forbid")
    media_ids: list[str] = Field(min_length=1, max_length=500)
    scope: MediaScope
    preset_id: str
    preserve_audio: bool | None = None
    preserve_subtitles: bool = True
    replace_source: bool = False


class PriorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: Priority


class QueueJob(BaseModel):
    id: str
    media_id: str
    scope: MediaScope
    name: str
    backend: EncoderBackend
    preset: CompressionPreset
    preserve_audio: bool
    requested_preserve_audio: bool | None = None
    preserve_subtitles: bool
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
