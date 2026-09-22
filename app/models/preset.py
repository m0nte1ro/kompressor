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
    "max_480p",
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

    target_video_bitrate: int | None = Field(default=None, gt=0, le=200_000_000)
    rate_control: Literal["abr", "crf", "icq"] = "abr"
    quality_value: float | None = Field(default=None, ge=0, le=51)
    encoder_preset: Literal["fast", "medium", "slow", "slower"] = "slow"
    output_bit_depth: Literal[8, 10] = 10
    source_resolutions: list[Literal["480p", "720p", "1080p", "2160p"]] = Field(default_factory=list)
    hdr_support: Literal["sdr_only", "hdr10_experimental"] = "sdr_only"
    planning_video_bitrate_low: int | None = Field(default=None, gt=0, le=200_000_000)
    planning_video_bitrate_high: int | None = Field(default=None, gt=0, le=200_000_000)
    stereo_audio_bitrate: int = Field(default=192000, ge=64000, le=320000)

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
        if self.rate_control == "abr":
            if self.target_video_bitrate is None or self.quality_value is not None:
                raise ValueError("ABR requires a target bitrate and no quality value.")
        else:
            expected = "cpu" if self.rate_control == "crf" else "qsv"
            if self.backend != expected:
                raise ValueError(f"{self.rate_control.upper()} requires backend {expected}.")
            if self.quality_value is None or self.target_video_bitrate is not None:
                raise ValueError("Quality mode requires a quality value and no target bitrate.")
            if self.rate_control == "icq" and (self.quality_value < 1 or self.quality_value % 1):
                raise ValueError("ICQ quality must be an integer from 1 to 51.")
            if not self.planning_video_bitrate_low or not self.planning_video_bitrate_high:
                raise ValueError("Quality mode requires a planning bitrate range, not an encoder target.")
        if (self.planning_video_bitrate_low is None) != (self.planning_video_bitrate_high is None):
            raise ValueError("Supply both planning bitrate bounds.")
        if self.planning_video_bitrate_low and self.planning_video_bitrate_low > self.planning_video_bitrate_high:
            raise ValueError("Planning bitrate range must be ordered.")
        if self.hdr_support == "hdr10_experimental" and (not self.preserve_hdr_metadata or self.output_bit_depth != 10):
            raise ValueError("Experimental HDR10 requires 10-bit output and metadata preservation.")
        return self


class CompressionPreset(PresetSettings):
    id: str
