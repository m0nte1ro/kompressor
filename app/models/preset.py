from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PresetScope = Literal[
    "movie",
    "show",
]

PresetIntent = Literal[
    "preserve_quality",
    "streaming_quality",
]

PresetOrigin = Literal[
    "built_in",
    "custom",
]

EncoderBackend = Literal[
    "cpu",
    "qsv",
]

DestinationCodec = Literal[
    "hevc",
    "av1",
]

TargetResolution = Literal[
    "keep",
    "max_2160p",
    "max_1080p",
    "max_720p",
    "max_576p",
    "max_480p",
]

SUPPORTED_SOURCE_RESOLUTIONS = ("480p", "576p", "720p", "1080p", "2160p")

AudioPolicy = Literal[
    "preserve",
    "efficient",
]

AudioConversionPolicy = Literal[
    "preserve",
    "efficient",
]

HDRPolicy = Literal[
    "preserve_source",
    "tone_map_to_sdr",
]


class HDRMetadataPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validate_signalling: bool = True
    preserve_color_primaries: bool = True
    preserve_transfer_characteristics: bool = True
    preserve_matrix_coefficients: bool = True
    preserve_mastering_display_metadata: bool = True
    preserve_max_cll: bool = True
    preserve_max_fall: bool = True


class EfficientAudioRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mono_stereo_codec: Literal["aac"] = "aac"
    multichannel_codec: Literal["eac3"] = "eac3"
    channel_handling: Literal["preserve", "downmix_stereo"] = "preserve"
    copy_codecs: list[Literal["aac", "eac3", "ac3"]] = Field(
        default_factory=lambda: ["aac", "eac3", "ac3"],
    )
    copy_if_bitrate_at_or_below_target: bool = True
    copy_unknown_bitrate: bool = True
    copy_channels_above: int | None = Field(default=None, ge=2, le=16)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_channel_field(cls, value):
        if not isinstance(value, dict) or "preserve_channels" not in value:
            return value
        normalized = dict(value)
        preserve_channels = normalized.pop("preserve_channels")
        normalized.setdefault("channel_handling", "preserve" if preserve_channels else "downmix_stereo")
        return normalized


class PresetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)

    scope: PresetScope
    intent: PresetIntent = "streaming_quality"
    origin: PresetOrigin = "custom"

    enabled: bool = True

    backend: EncoderBackend

    destination_codec: DestinationCodec

    target_video_bitrate: int | None = Field(default=None, gt=0, le=200_000_000)
    rate_control: Literal["abr", "crf", "icq"] = "abr"
    quality_value: float | None = Field(default=None, ge=0, le=51)
    encoder_preset: Literal["fast", "medium", "slow", "slower"] = "slow"
    output_bit_depth: Literal[8, 10] = 10
    source_resolutions: list[Literal["480p", "576p", "720p", "1080p", "2160p"]] = Field(
        default_factory=lambda: list(SUPPORTED_SOURCE_RESOLUTIONS),
    )
    hdr_support: Literal["sdr_only", "hdr10_experimental"] = "hdr10_experimental"
    hdr_policy: HDRPolicy = "preserve_source"
    hdr_metadata: HDRMetadataPolicy = Field(default_factory=HDRMetadataPolicy)
    planning_video_bitrate_low: int | None = Field(default=None, gt=0, le=200_000_000)
    planning_video_bitrate_high: int | None = Field(default=None, gt=0, le=200_000_000)
    stereo_audio_bitrate: int = Field(default=192000, ge=64000, le=320000)

    target_resolution: TargetResolution = "keep"

    audio_policy: AudioPolicy = "preserve"

    preserve_audio_by_default: bool = True

    audio_conversion_policy: AudioConversionPolicy = "preserve"

    target_audio_bitrate: int | None = Field(default=None, gt=0, le=10_000_000)

    preserve_hdr_metadata: bool = True

    efficient_audio_rules: EfficientAudioRules = Field(default_factory=EfficientAudioRules)

    minimum_source_bitrate: int = Field(default=0, ge=0, le=500_000_000)

    minimum_expected_saving_percent: float = Field(default=20.0, ge=0, lt=100)

    allow_hevc_reencode: bool = False

    @model_validator(mode="before")
    @classmethod
    def fill_quality_planning_defaults(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if (
            normalized.get("rate_control") in {"crf", "icq"}
            and "planning_video_bitrate_low" not in normalized
            and "planning_video_bitrate_high" not in normalized
        ):
            scope = normalized.get("scope")
            intent = normalized.get("intent", "streaming_quality")
            if intent == "preserve_quality":
                low, high = (1_000_000, 30_000_000) if scope == "movie" else (1_000_000, 20_000_000)
            else:
                low, high = (2_000_000, 8_000_000) if scope == "movie" else (1_000_000, 6_000_000)
            normalized["planning_video_bitrate_low"] = low
            normalized["planning_video_bitrate_high"] = high
        if normalized.get("hdr_policy") == "tone_map_to_sdr" and normalized.get("preserve_hdr_metadata") is False:
            normalized["hdr_metadata"] = {
                "validate_signalling": False,
                "preserve_color_primaries": False,
                "preserve_transfer_characteristics": False,
                "preserve_matrix_coefficients": False,
                "preserve_mastering_display_metadata": False,
                "preserve_max_cll": False,
                "preserve_max_fall": False,
            }
        return normalized

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_resolution_policy(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        legacy = normalized.pop("resolution_policy", None)
        if "target_resolution" not in normalized and legacy is not None:
            normalized["target_resolution"] = {
                "preserve": "keep",
                "max_2160p": "max_2160p",
                "max_1080p": "max_1080p",
                "max_720p": "max_720p",
                "max_576p": "max_576p",
                "max_480p": "max_480p",
            }.get(legacy, legacy)
        elif normalized.get("target_resolution") == "preserve":
            normalized["target_resolution"] = "keep"
        return normalized

    @model_validator(mode="after")
    def efficient_audio_requires_bitrate(self):
        if (self.audio_policy == "efficient" or self.audio_conversion_policy == "efficient") and self.target_audio_bitrate is None:
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
        if (
            self.planning_video_bitrate_low is not None
            and self.planning_video_bitrate_high is not None
            and self.planning_video_bitrate_low > self.planning_video_bitrate_high
        ):
            raise ValueError("Planning bitrate range must be ordered.")
        if self.hdr_support == "hdr10_experimental" and (not self.preserve_hdr_metadata or self.output_bit_depth != 10):
            raise ValueError("Experimental HDR10 requires 10-bit output and metadata preservation.")
        if self.hdr_policy == "tone_map_to_sdr":
            if self.hdr_support == "sdr_only":
                raise ValueError("Tone mapping requires HDR10 input support.")
            if self.origin == "built_in":
                raise ValueError("Built-in presets must preserve source HDR mode.")
            if self.preserve_hdr_metadata or any(self.hdr_metadata.model_dump().values()):
                raise ValueError("Tone mapping to SDR cannot preserve HDR metadata.")
        return self


class CompressionPreset(PresetSettings):
    id: str
