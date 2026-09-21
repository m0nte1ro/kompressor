from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


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


class CompressionPreset(BaseModel):
    id: str
    name: str

    scope: PresetScope

    enabled: bool = True

    backend: EncoderBackend

    destination_codec: DestinationCodec

    target_video_bitrate: int

    resolution_policy: ResolutionPolicy

    audio_policy: AudioPolicy

    target_audio_bitrate: int | None = None

    preserve_hdr_metadata: bool = True

    minimum_source_bitrate: int

    minimum_expected_saving_percent: float = 20.0

    allow_hevc_reencode: bool = False
