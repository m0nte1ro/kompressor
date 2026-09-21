from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MediaScope = Literal["movie", "show"]
HDRType = Literal[
    "hdr10",
    "dolby_vision",
    "dolby_vision_hdr10",
]


class AudioTrack(BaseModel):
    codec: str
    channels: int
    bitrate: int | None = None
    language: str | None = None
    title: str | None = None


class Movie(BaseModel):
    id: str
    name: str
    year: int | None = None
    path: str

    source: str | None = None

    width: int
    height: int
    resolution: str

    video_codec: str
    video_bitrate: int

    duration_seconds: float

    hdr: HDRType | None = None
    interlaced: bool = False

    size: int

    audio: list[AudioTrack] = Field(
        default_factory=list,
    )

    hardlinks: int = 1

    tags: list[str] = Field(
        default_factory=list,
    )


class Episode(BaseModel):
    id: str

    season: int
    episode: int

    title: str | None = None
    path: str

    width: int
    height: int
    resolution: str

    video_codec: str
    video_bitrate: int

    duration_seconds: float

    hdr: HDRType | None = None
    interlaced: bool = False

    size: int

    audio: list[AudioTrack] = Field(
        default_factory=list,
    )

    hardlinks: int = 1

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
