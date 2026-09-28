"""VA-API video arguments shared by real jobs and the startup smoke encode."""
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.probe import StreamFacts


def hardware_decode(stream: StreamFacts) -> bool:
    """Conservative Intel UHD 730 decode allowlist; unknown facts use software."""
    profile = (stream.profile or '').lower()
    pixel = stream.pixel_format
    if stream.dispositions.get('attached_pic'):
        return False
    if stream.codec == 'h264':
        return pixel == 'yuv420p' and profile in {'baseline', 'constrained baseline', 'main', 'high'}
    if stream.codec == 'hevc':
        return (profile == 'main' and pixel == 'yuv420p'
                or profile == 'main 10' and pixel in {'yuv420p', 'yuv420p10le'})
    if stream.codec == 'mpeg2video':
        return pixel == 'yuv420p' and profile in {'simple', 'main'}
    if stream.codec == 'vc1':
        return pixel == 'yuv420p' and profile in {'simple', 'main', 'advanced'}
    if stream.codec == 'vp9':
        return (profile == 'profile 0' and pixel == 'yuv420p'
                or profile == 'profile 2' and pixel == 'yuv420p10le')
    return False


def device_args(device: Path) -> list[str]:
    return ['-init_hw_device', f'vaapi=va:{device}', '-filter_hw_device', 'va']


def video_args(depth: int, rate_control: str, quality: float | None,
               bitrate: int | None, *, hardware: bool = False) -> list[str]:
    if hardware:
        filters = 'scale_vaapi=format=p010' if depth == 10 else 'scale_vaapi=format=nv12'
    else:
        filters = 'format=p010le,hwupload' if depth == 10 else 'format=nv12,hwupload'
    args = ['-filter:v:0', filters, '-c:v:0', 'hevc_vaapi',
            '-profile:v:0', 'main10' if depth == 10 else 'main']
    if rate_control == 'icq' and quality is not None:
        return args + ['-rc_mode:v:0', 'ICQ', '-global_quality:v:0', str(int(quality))]
    if rate_control == 'qvbr' and quality is not None and bitrate is not None:
        return args + ['-rc_mode:v:0', 'QVBR', '-b:v:0', str(bitrate),
                       '-global_quality:v:0', str(int(quality))]
    if rate_control == 'abr' and bitrate is not None:
        return args + ['-rc_mode:v:0', 'VBR', '-b:v:0', str(bitrate)]
    raise ValueError(f'Unsupported GPU video rate control: {rate_control}.')


def _failure_detail(stderr: str, stdout: str) -> str:
    """Keep the first FFmpeg error, which often precedes generic shutdown noise."""
    detail = (stderr or stdout).strip()
    if len(detail) > 2400:
        return f'{detail[:1600]}\n...\n{detail[-800:]}'
    return detail or 'ffmpeg failed without diagnostics'


def smoke_check(binary: str, device: Path, ffprobe: str, workspace: Path) -> tuple[bool, str | None]:
    """Bounded Main10/QVBR encode and decoded-frame verification; no rate fallback."""
    prefix = 'GPU VA-API Main10 QVBR smoke test failed'
    try:
        with TemporaryDirectory(prefix='.vaapi-smoke-', dir=workspace / 'jobs') as temporary:
            output = Path(temporary) / 'smoke.mkv'
            command = [binary, '-hide_banner', '-nostdin', '-v', 'error', *device_args(device),
                       '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30',
                       '-frames:v', '10', '-an', *video_args(10, 'qvbr', 23, 4_000_000),
                       '-f', 'matroska', str(output)]
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=15, check=False)
            if result.returncode:
                return False, f'{prefix}: {_failure_detail(result.stderr, result.stdout)}'
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                 '-count_frames', '-show_entries', 'stream=codec_name,profile,pix_fmt,nb_read_frames',
                 '-of', 'json', str(output)], stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=15, check=False)
            if result.returncode:
                return False, f'{prefix}: ffprobe: {_failure_detail(result.stderr, result.stdout)}'
            streams = json.loads(result.stdout).get('streams', [])
            if len(streams) != 1:
                return False, f'{prefix}: expected one HEVC video stream.'
            stream = streams[0]
            if (stream.get('codec_name') != 'hevc' or stream.get('profile') != 'Main 10'
                    or stream.get('pix_fmt') != 'yuv420p10le' or int(stream.get('nb_read_frames', 0)) <= 0):
                return False, f'{prefix}: output must be HEVC Main 10/yuv420p10le with decoded frames.'
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as error:
        return False, f'{prefix}: {error}'
    return True, None
