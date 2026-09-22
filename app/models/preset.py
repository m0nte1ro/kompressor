from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PresetScope = Literal[
    "movie",
    "show",
]

EncoderBackend = Literal[
    "cpu",
    "qsv",
]

DestinationCodec = Literal[
    "hevc",
    "av1",
]

ResolutionPolicy = Literal[
    "preserve",
    "max_1080p",
    "max_720p",
]

AudioPolicy = Literal[
    "preserve",
    "efficient",
]


class PresetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)

    scope: PresetScope

    enabled: bool = True

    backend: EncoderBackend

    destination_codec: DestinationCodec

    target_video_bitrate: int = Field(gt=0, le=200_000_000)

    resolution_policy: ResolutionPolicy

    audio_policy: AudioPolicy

    target_audio_bitrate: int | None = Field(default=None, gt=0, le=10_000_000)

    preserve_hdr_metadata: bool = True

    minimum_source_bitrate: int = Field(ge=0, le=500_000_000)

    minimum_expected_saving_percent: float = Field(default=20.0, ge=0, lt=100)

    allow_hevc_reencode: bool = False

    @model_validator(mode="after")
    def efficient_audio_requires_bitrate(self):
        if self.audio_policy == "efficient" and self.target_audio_bitrate is None:
            raise ValueError("Efficient audio requires a target audio bitrate.")
        return self


class CompressionPreset(PresetSettings):
    id: str
