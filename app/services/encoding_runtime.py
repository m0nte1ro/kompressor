"""Read-only startup checks for real CPU and Intel QSV encoder capabilities."""
import os
import shutil
import stat
from pathlib import Path

from app.services.ffprobe import FFprobeService
from app.workers.ffmpeg import FFmpegEncoder


def executable(binary: str) -> str | None:
    resolved = shutil.which(binary)
    return str(Path(resolved).resolve()) if resolved else None


def workspace_writable(root: Path) -> bool:
    jobs = root / "jobs"
    try:
        if root.is_symlink() or jobs.is_symlink() or not root.is_dir() or not jobs.is_dir():
            return False
        flags = os.statvfs(jobs).f_flag
        if flags & getattr(os, "ST_RDONLY", 1):
            return False
        mode = jobs.stat().st_mode
        return bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)) and os.access(jobs, os.W_OK | os.X_OK)
    except OSError:
        return False


def roots_overlap_workspace(workspace_root: Path, media_roots: dict[str, Path]) -> bool:
    try:
        workspace = workspace_root.expanduser().resolve()
        return any(
            workspace == root.resolve() or workspace.is_relative_to(root.resolve())
            or root.resolve().is_relative_to(workspace)
            for root in media_roots.values()
        )
    except OSError:
        return True


def capability_status(ffmpeg_binary: str, ffprobe_binary: str, workspace_root: Path,
                      media_roots: dict[str, Path],
                      qsv_device: Path = Path("/dev/dri/renderD128")) -> dict:
    common_reasons: list[str] = []
    ffmpeg = executable(ffmpeg_binary)
    ffprobe = executable(ffprobe_binary)

    if ffmpeg is None:
        common_reasons.append(f"ffmpeg executable was not found: {ffmpeg_binary}")

    ffprobe_ready = False
    if ffprobe is None:
        common_reasons.append(f"ffprobe executable was not found: {ffprobe_binary}")
    else:
        ffprobe_ready, ffprobe_error = FFprobeService.runtime_check(ffprobe)
        if not ffprobe_ready and ffprobe_error:
            common_reasons.append(ffprobe_error)

    root = workspace_root.expanduser().absolute()
    writable = workspace_writable(root)
    if not writable:
        common_reasons.append(f"Workspace jobs directory is missing or not writable: {root / 'jobs'}")
    try:
        resolved = root.resolve()
        for media_root in media_roots.values():
            media = media_root.resolve()
            if resolved == media or resolved.is_relative_to(media) or media.is_relative_to(resolved):
                common_reasons.append("Workspace and media roots must not overlap.")
                break
    except OSError:
        common_reasons.append("Workspace or media root cannot be resolved.")

    x265_available = False
    cpu_error = None
    if ffmpeg is not None:
        x265_available, cpu_error = FFmpegEncoder.runtime_check(ffmpeg)
    cpu_reasons = [*common_reasons]
    if not x265_available:
        cpu_reasons.append(cpu_error or "libx265 is unavailable.")

    qsv_available = False
    qsv_error = None
    qsv_path = qsv_device.expanduser().absolute()
    if ffmpeg is not None:
        qsv_available, qsv_error = FFmpegEncoder.qsv_runtime_check(ffmpeg, qsv_path)
    qsv_reasons = [*common_reasons]
    if not qsv_available:
        qsv_reasons.append(qsv_error or "Intel QSV HEVC is unavailable.")

    cpu_ready = not cpu_reasons
    qsv_ready = not qsv_reasons
    supported = [
        backend for backend, ready in (("cpu", cpu_ready), ("qsv", qsv_ready)) if ready
    ]
    overall_reason = None if supported else "; ".join(dict.fromkeys([*cpu_reasons, *qsv_reasons]))

    return {
        "available": bool(supported),
        "cpu_available": cpu_ready,
        "qsv_available": qsv_ready,
        "supported_backends": supported,
        "ffmpeg_available": ffmpeg is not None,
        "ffprobe_available": ffprobe_ready,
        "libx265_available": x265_available,
        "hevc_qsv_available": qsv_available,
        "qsv_device": str(qsv_path),
        "ffmpeg_binary": ffmpeg or ffmpeg_binary,
        "ffprobe_binary": ffprobe or ffprobe_binary,
        "workspace_root": str(root),
        "workspace_writable": writable,
        "cpu_unavailable_reason": "; ".join(dict.fromkeys(cpu_reasons)) if cpu_reasons else None,
        "qsv_unavailable_reason": "; ".join(dict.fromkeys(qsv_reasons)) if qsv_reasons else None,
        "unavailable_reason": overall_reason,
    }
