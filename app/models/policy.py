from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field


class EligibilityResult(BaseModel):
    eligible: bool

    reasons: list[str]
    warnings: list[str]

    preset_id: str
    preset_name: str

    backend: str
    destination_codec: str

    source_size: int

    estimated_output_size: int | None
    estimated_saving: int | None
    estimated_saving_percent: float | None

    preserve_audio: bool
    preserve_subtitles: bool
    estimated_output_size_low: int | None = None
    estimated_output_size_high: int | None = None
    estimated_saving_low: int | None = None
    estimated_saving_high: int | None = None
    estimate_basis: str = "bitrate"
    planning_output_size: int | None = None
    planning_saving: int | None = None
    planning_saving_percent: float | None = None
    audio_plan: list[dict] = Field(default_factory=list)
