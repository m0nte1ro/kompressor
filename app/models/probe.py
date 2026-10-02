"""Normalized probe facts, independent of Movie/Show identity and ffprobe JSON."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


def resolution_class(width: int | None, height: int | None) -> int | None:
    """Classify nominal rasters, allowing small edge crops and letterboxing.

    At least one dimension must be within 2% below its nominal raster edge;
    neither may exceed that raster. Raw dimensions remain unchanged.
    """
    if width is None or height is None:
        return None
    if height in (480, 576) and width <= 1024:
        return height
    for w, h in [(3840, 2160), (1920, 1080), (1280, 720), (720, 576), (640, 480)]:
        if width <= w and height <= h and (width * 100 >= w * 98 or height * 100 >= h * 98):
            return h
    return None


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
    profile: str | None = None
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
    # Set in memory by with_inferred_scan_type(); never persisted.
    scan_type_inferred: bool = False
    frame_rate: str | None = None
    pixel_format: str | None = None
    color_range: Literal["tv", "pc"] | None = None
    field_order: Literal["top_first", "bottom_first", "unknown"] = "unknown"
    hdr: HDRSignalling | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def infer_missing_resolution_class(self):
        # Also applies to cached observations created by the old exact-edge
        # classifier, without re-probing or changing source revisions.
        if self.kind == "video" and self.resolution_class is None:
            self.resolution_class = resolution_class(self.width, self.height)
        return self

    @model_validator(mode="after")
    def infer_legacy_full_range(self):
        # Older persisted probes predate the explicit colour-range field. FFmpeg's
        # yuvj formats are the legacy full-range JPEG variants, so this inference
        # is safe and lets those observations retain their range without a rescan.
        if self.color_range is None and self.pixel_format in {
            "yuvj420p", "yuvj422p", "yuvj444p",
        }:
            self.color_range = "pc"
        return self


ABSENT_COLOUR_VALUES = {None, "unknown", "unspecified"}


def mapped_kinds(preserve_subtitles: bool) -> frozenset[str]:
    """Stream kinds copied into the Matroska output.

    Data streams (e.g. MP4/MOV timecode tracks) are never mapped: the Matroska
    muxer rejects them, and they carry nothing a player uses.
    """
    if preserve_subtitles:
        return frozenset({"video", "audio", "subtitle", "attachment"})
    return frozenset({"video", "audio"})


def _untagged_8bit_progressive_sdr(stream: StreamFacts) -> tuple[str, str, str] | None:
    """Shared evidence checks for the per-codec untagged-SDR rules below.

    Requires progressive 8-bit yuv420p video, all three colour fields absent and no
    mastering-display, content-light, Dolby Vision or HDR10+ evidence. Values follow
    the usual BT.709 (HD) / BT.601 (625- or 525-line SD) player assumption and are
    written explicitly on output.
    """
    hdr = stream.hdr
    if (stream.kind != "video" or hdr is None or stream.scan_type != "progressive"
            or stream.pixel_format != "yuv420p" or hdr.bit_depth != 8):
        return None
    if not {hdr.transfer, hdr.primaries, hdr.matrix} <= ABSENT_COLOUR_VALUES:
        return None
    if (hdr.mastering_display is not None or hdr.content_light is not None
            or hdr.dolby_vision_config is not None
            or hdr.dolby_vision_rpu is True or hdr.hdr10plus_metadata is True):
        return None
    if stream.width is None or stream.height is None:
        return None
    if stream.width > 1024 or stream.height > 576:
        return "bt709", "bt709", "bt709"
    if stream.height > 480:
        return "bt470bg", "smpte170m", "bt470bg"
    return "smpte170m", "smpte170m", "smpte170m"


def legacy_vc1_sdr_colours(stream: StreamFacts) -> tuple[str, str, str] | None:
    """VC-1 (SMPTE 421M) is 8-bit 4:2:0 with no PQ/HLG or HDR metadata carriage, so
    absent colour signalling cannot conceal HDR there."""
    return _untagged_8bit_progressive_sdr(stream) if stream.codec == "vc1" else None


H264_8BIT_PROFILES = {"baseline", "constrained baseline", "main", "high"}


def untagged_h264_sdr_colours(stream: StreamFacts) -> tuple[str, str, str] | None:
    """H.264 can carry HDR, but PQ/HLG in practice needs High 10 or above, and the
    shared check already requires 8-bit yuv420p. A known profile must still be
    8-bit Baseline/Main/High (High 10/4:2:2/4:4:4 stay blocked by name). An unknown
    profile is accepted because probes persisted before schema 4 have none and are
    not re-probed. Accepted residual risk: 8-bit HLG signalled solely by an
    alternative-transfer SEI with no VUI colour description is not detected."""
    if stream.codec != "h264":
        return None
    if stream.profile is not None and stream.profile.lower() not in H264_8BIT_PROFILES:
        return None
    return _untagged_8bit_progressive_sdr(stream)


def assumed_sdr_colours(stream: StreamFacts) -> tuple[str, str, str] | None:
    """Assumed (primaries, transfer, matrix) for an untagged source a codec-specific
    rule accepts as SDR, else None. HEVC, AV1 and every other codec have no rule and
    keep the normal confirmed-SDR requirement."""
    return legacy_vc1_sdr_colours(stream) or untagged_h264_sdr_colours(stream)


class ChapterFacts(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    title: str | None = None


class MediaProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container: str
    container_bitrate: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)
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


# Release names say "1080i" for interlaced broadcast captures; "1080p" or no
# tag at all is the overwhelmingly common progressive case.
INTERLACED_NAME = re.compile(r"(?<![0-9])(?:480|576|720|1080)i(?![a-z0-9])", re.IGNORECASE)


def with_inferred_scan_type(probe: MediaProbeResult, name: str) -> MediaProbeResult:
    """Resolve an unsignalled scan type instead of blocking the file.

    Many progressive MKVs carry no field order and ffprobe's frame sample cannot
    prove a whole stream progressive. Interlacing that is signalled, or a
    "1080i"-style name, still counts as interlaced; otherwise a video stream with
    known dimensions is treated as progressive.
    """
    def resolve(stream: StreamFacts) -> StreamFacts:
        if stream.kind != "video" or stream.scan_type != "unknown":
            return stream
        if INTERLACED_NAME.search(name):
            return stream.model_copy(update={"scan_type": "interlaced", "scan_type_inferred": True})
        if stream.width is None or stream.height is None:
            return stream
        return stream.model_copy(update={"scan_type": "progressive", "scan_type_inferred": True})

    if not any(s.kind == "video" and s.scan_type == "unknown" for s in probe.streams):
        return probe
    return probe.model_copy(update={"streams": [resolve(s) for s in probe.streams]})
