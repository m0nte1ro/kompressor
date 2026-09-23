from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from app.models.probe import MediaProbeResult


MediaScope = Literal["movie", "show"]
HDRType = Literal[
    "hdr10",
    "hdr10plus",
    "hlg",
    "unknown",
    "dolby_vision",
    "dolby_vision_hdr10",
]


class AudioTrack(BaseModel):
    codec: str
    channels: int | None
    bitrate: int | None = None
    language: str | None = None
    title: str | None = None
    stream_index: int | None = None
    channel_layout: str | None = None
    dispositions: dict[str, bool] = Field(default_factory=dict)


class Movie(BaseModel):
    media_id: str | None = None
    revision_id: str | None = None
    processing_supported: bool = True
    probe: MediaProbeResult | None = None
    id: str
    name: str
    year: int | None = None
    path: str

    source: str | None = None

    width: int | None
    height: int | None
    resolution: str

    video_codec: str
    video_bitrate: int | None
    video_bitrate_estimated: bool = False

    duration_seconds: float | None

    hdr: HDRType | None = None
    interlaced: bool | None = False

    size: int

    audio: list[AudioTrack] = Field(
        default_factory=list,
    )

    hardlinks: int | None = 1

    tags: list[str] = Field(
        default_factory=list,
    )


class Episode(BaseModel):
    media_id: str | None = None
    revision_id: str | None = None
    processing_supported: bool = True
    probe: MediaProbeResult | None = None
    id: str

    season: int
    episode: int

    title: str | None = None
    path: str

    width: int | None
    height: int | None
    resolution: str

    video_codec: str
    video_bitrate: int | None
    video_bitrate_estimated: bool = False

    duration_seconds: float | None

    hdr: HDRType | None = None
    interlaced: bool | None = False

    size: int

    audio: list[AudioTrack] = Field(
        default_factory=list,
    )

    hardlinks: int | None = 1

    tags: list[str] = Field(
        default_factory=list,
    )


class Season(BaseModel):
    season: int

    tags: list[str] = Field(
        default_factory=list,
    )

    episodes: list[Episode] = Field(
        default_factory=list,
    )


class Show(BaseModel):
    id: str
    name: str

    tags: list[str] = Field(
        default_factory=list,
    )

    seasons: list[Season] = Field(
        default_factory=list,
    )


class MediaLibrary(BaseModel):
    movies: list[Movie] = Field(
        default_factory=list,
    )

    shows: list[Show] = Field(
        default_factory=list,
    )


def spatial_resolution(item: Movie | Episode) -> str:
    """Canonical applicability label, independent of progressive/interlaced scan.

    Keep the source's display label and separate interlaced flag intact. Do not
    infer a new resolution from height: cropped 1080 sources can be shorter.
    """
    if item.probe:
        video = next((s for s in item.probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)
        if video and video.resolution_class:
            return f"{video.resolution_class}p"
    label = item.resolution.lower()
    if label.endswith("i") and label[:-1].isdigit():
        return label[:-1] + "p"
    return label
