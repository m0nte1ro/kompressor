"""Normalized probe facts, independent of Movie/Show identity and ffprobe JSON."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class HDRSignalling(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transfer: str | None = None
    primaries: str | None = None
    matrix: str | None = None
    bit_depth: int | None = Field(default=None, gt=0)
    mastering_display: dict[str, JsonValue] | None = None
    content_light: dict[str, JsonValue] | None = None
    dolby_vision_config: dict[str, JsonValue] | None = None
    dolby_vision_rpu: bool | None = None
    hdr10plus_metadata: bool | None = None
    # False means absence of dynamic metadata has NOT been established.
    inspection_complete: bool = False

    def classify(self) -> "HDRClassification":
        base: Literal["sdr", "hdr10", "hlg", "unknown"] = "unknown"
        if self.transfer == "smpte2084" and self.primaries == "bt2020" and self.bit_depth in (10, 12):
            base = "hdr10"
        elif self.transfer == "arib-std-b67":
            base = "hlg"
        elif self.transfer in ("bt709", "smpte170m", "bt470bg"):
            base = "sdr"
        dv = self.dolby_vision_config is not None or self.dolby_vision_rpu is True
        plus = self.hdr10plus_metadata is True
        # PQ/BT.2020 alone does not establish an HDR10-compatible DV base layer.
        # Preserve DV configuration for a future validated profile classifier.
        if dv:
            base = "unknown"
        known = self.inspection_complete and self.dolby_vision_rpu is not None and self.hdr10plus_metadata is not None
        return HDRClassification(base=base, dolby_vision=dv, hdr10plus=plus,
                                 uncertain=not known or base == "unknown")


class HDRClassification(BaseModel):
    base: Literal["sdr", "hdr10", "hlg", "unknown"]
    dolby_vision: bool
    hdr10plus: bool
    uncertain: bool



class StreamFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    kind: Literal["video", "audio", "subtitle", "attachment", "data"]
    codec: str
    language: str | None = None
    title: str | None = None
    dispositions: dict[str, bool] = Field(default_factory=dict)
    channels: int | None = Field(default=None, gt=0)
    channel_layout: str | None = None
    sample_rate: int | None = Field(default=None, gt=0)
    bitrate: int | None = Field(default=None, gt=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    resolution_class: int | None = Field(default=None, gt=0)
    scan_type: Literal["progressive", "interlaced", "mixed", "unknown"] = "unknown"
    field_order: Literal["top_first", "bottom_first", "unknown"] = "unknown"
    hdr: HDRSignalling | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ChapterFacts(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    title: str | None = None


class MediaProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container: str
    duration_seconds: float = Field(gt=0)
    streams: list[StreamFacts] = Field(default_factory=list)
    chapters: list[ChapterFacts] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_streams(self):
        indices = [stream.index for stream in self.streams]
        if len(indices) != len(set(indices)):
            raise ValueError("Stream indices must be unique within a file revision.")
        if any(ch.end_seconds < ch.start_seconds for ch in self.chapters):
            raise ValueError("Chapter end precedes start.")
        return self
