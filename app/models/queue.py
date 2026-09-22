from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.media import MediaScope
from app.models.preset import CompressionPreset, EncoderBackend


JobStatus = Literal["queued", "encoding", "validating", "completed", "skipped", "blocked"]
Priority = Literal["low", "normal", "high", "urgent"]


class EnqueueRequest(BaseModel):
    # The client supplies a preset ID, never technical encoder overrides.
    model_config = ConfigDict(extra="forbid")
    media_ids: list[str] = Field(min_length=1, max_length=500)
    scope: MediaScope
    preset_id: str
    preserve_audio: bool = True
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
    estimated_output_size: int
    estimated_saving: int
    source_codec: str
    replace_source: bool = False
    estimate_basis: str = "bitrate"
    estimated_saving_low: int | None = None
    estimated_saving_high: int | None = None
    priority: Priority = "normal"
    move_next_order: int = 0
    status: JobStatus = "queued"
    progress: float = 0
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float = 0
    reasons: list[str] = Field(default_factory=list)
