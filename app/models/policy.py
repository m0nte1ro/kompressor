from __future__ import annotations

from pydantic import BaseModel


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
