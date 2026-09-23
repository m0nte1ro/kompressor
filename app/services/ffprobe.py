"""The only ffprobe adapter: bounded read-only subprocess and normalized JSON parsing."""
import json
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.models.probe import ChapterFacts, HDRSignalling, MediaProbeResult, StreamFacts
from app.services.errors import InvalidOperation


class TechnicalProbe(Protocol):
    def inspect(self, path: Path) -> MediaProbeResult: ...


def positive(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    number = positive(value)
    return int(number) if number is not None and number.is_integer() else None


def frame_rate(value: Any) -> str | None:
    try:
        rate = Fraction(str(value))
        return str(rate) if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def resolution_class(width: int | None, height: int | None) -> int | None:
    if width is None or height is None:
        return None
    if height in (480, 576) and width <= 1024:
        return height
    # Nominal raster classes include common letterboxed/cropped sources.
    for w, h in [(3840, 2160), (1920, 1080), (1280, 720), (720, 576), (640, 480)]:
        if width == w and height <= h or height == h and width <= w:
            return h
    return None


def bit_depth(raw: dict) -> int | None:
    depth = integer(raw.get('bits_per_raw_sample'))
    if depth is not None:
        return depth
    pixel = raw.get('pix_fmt', '')
    match = re.fullmatch(r'(?:yuv\d+p|gbrp|gray)(9|10|12|14|16)(?:le|be)', pixel)
    if match:
        return int(match[1])
    if pixel in {'yuv420p', 'yuv422p', 'yuv444p', 'yuvj420p', 'yuvj422p', 'yuvj444p', 'nv12', 'nv21', 'gray', 'rgb24', 'bgr24'}:
        return 8
    if pixel in {'p010le', 'p010be'}:
        return 10
    return None


def parse_stream(raw: dict, frames: list[dict]) -> StreamFacts:
    kind = raw.get('codec_type')
    if kind not in {'video', 'audio', 'subtitle', 'attachment', 'data'}:
        raise ValueError('Unsupported stream type in probe result.')
    tags = raw.get('tags') or {}
    index = int(raw['index'])
    sampled = [f for f in frames if f.get('stream_index') == index]
    facts = StreamFacts(index=index, kind=kind, codec=raw.get('codec_name') or 'unknown',
        language=tags.get('language'), title=tags.get('title'), metadata=tags,
        dispositions={k: bool(v) for k, v in (raw.get('disposition') or {}).items()},
        channels=integer(raw.get('channels')), channel_layout=raw.get('channel_layout'),
        sample_rate=integer(raw.get('sample_rate')), bitrate=integer(raw.get('bit_rate')),
        width=integer(raw.get('width')), height=integer(raw.get('height')),
        frame_rate=frame_rate(raw.get('avg_frame_rate')) or frame_rate(raw.get('r_frame_rate')),
        pixel_format=raw.get('pix_fmt'))
    if kind != 'video':
        return facts
    facts.resolution_class = resolution_class(facts.width, facts.height)
    order = raw.get('field_order')
    if order == 'progressive':
        facts.scan_type = 'progressive'
    elif order in {'tt', 'bb', 'tb', 'bt'}:
        facts.scan_type = 'interlaced'
        facts.field_order = 'top_first' if order in {'tt', 'bt'} else 'bottom_first'
    interlaced = {f['interlaced_frame'] for f in sampled if f.get('interlaced_frame') in (0, 1)}
    if 1 in interlaced:
        facts.scan_type = 'mixed' if 0 in interlaced else 'interlaced'
        first = {f.get('top_field_first') for f in sampled if f.get('interlaced_frame') == 1}
        if first == {1}:
            facts.field_order = 'top_first'
        elif first == {0}:
            facts.field_order = 'bottom_first'
    # A progressive sample alone cannot prove a whole unknown stream progressive.
    hdr = HDRSignalling(transfer=raw.get('color_transfer'), primaries=raw.get('color_primaries'),
                       matrix=raw.get('color_space'), bit_depth=bit_depth(raw))
    side_data = [*(raw.get('side_data_list') or []), *(s for f in sampled for s in f.get('side_data_list', []))]
    for side in side_data:
        name = side.get('side_data_type', '').lower()
        data = {k: v for k, v in side.items() if k != 'side_data_type'}
        if 'mastering display' in name:
            hdr.mastering_display = data
        elif 'content light' in name:
            hdr.content_light = data
        elif 'dovi configuration' in name:
            hdr.dolby_vision_config = data
            if side.get('rpu_present_flag') == 1:
                hdr.dolby_vision_rpu = True
        elif 'dolby vision' in name:
            hdr.dolby_vision_rpu = True
        elif '2094-40' in name or 'hdr10+' in name:
            hdr.hdr10plus_metadata = True
    # Sampling can establish presence, not absence, of dynamic HDR metadata.
    facts.hdr = hdr
    return facts


def parse_ffprobe(payload: dict) -> MediaProbeResult:
    try:
        info = payload.get('format') or {}
        frames = payload.get('frames') or []
        chapters = []
        for chapter in payload.get('chapters') or []:
            start, end = chapter.get('start_time'), chapter.get('end_time')
            if start is not None and end is not None:
                chapters.append(ChapterFacts(start_seconds=float(start), end_seconds=float(end),
                                             title=(chapter.get('tags') or {}).get('title')))
        return MediaProbeResult(container=info.get('format_name') or 'unknown',
            container_bitrate=integer(info.get('bit_rate')),
            duration_seconds=positive(info.get('duration')),
            streams=[parse_stream(s, frames) for s in payload.get('streams') or []],
            chapters=chapters, metadata=info.get('tags') or {})
    except (AttributeError, KeyError, TypeError, ValueError, ValidationError) as error:
        raise InvalidOperation(f'Invalid ffprobe metadata: {error}') from error


class FFprobeService:
    @staticmethod
    def runtime_check(binary: str) -> tuple[bool, str | None]:
        try:
            result = subprocess.run([binary, '-version'], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f'Could not run ffprobe version check: {error}'
        version_output = (result.stdout + result.stderr)[:200].lower()
        if result.returncode != 0 or 'ffprobe version' not in version_output:
            return False, 'Configured ffprobe did not return a valid version response.'
        return True, None

    def __init__(self, binary: str = 'ffprobe', timeout: float = 30):
        self.binary = binary
        self.timeout = timeout

    def inspect(self, path: Path) -> MediaProbeResult:
        command = [self.binary, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                   '-show_format', '-show_streams', '-show_chapters', '-show_frames',
                   '-read_intervals', '%+#48', '-show_entries',
                   'format:stream:chapter:frame=stream_index,interlaced_frame,top_field_first:frame_side_data',
                   '-of', 'json', '-i', str(path.absolute())]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=self.timeout, check=False)
        except FileNotFoundError as error:
            raise InvalidOperation(f'ffprobe executable not found: {self.binary}') from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InvalidOperation(f'ffprobe failed: {error}') from error
        if result.returncode:
            raise InvalidOperation('ffprobe could not read this file: ' + result.stderr[-500:])
        try:
            return parse_ffprobe(json.loads(result.stdout))
        except json.JSONDecodeError as error:
            raise InvalidOperation('ffprobe returned invalid JSON.') from error
