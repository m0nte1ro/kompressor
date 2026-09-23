"""Read-only startup checks for the deliberately narrow real encoder capability."""
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
                      media_roots: dict[str, Path]) -> dict:
    reasons = []
    ffmpeg = executable(ffmpeg_binary)
    ffprobe = executable(ffprobe_binary)
    if ffmpeg is None:
        reasons.append(f"ffmpeg executable was not found: {ffmpeg_binary}")
    if ffprobe is None:
        reasons.append(f"ffprobe executable was not found: {ffprobe_binary}")
        ffprobe_ready = False
    else:
        ffprobe_ready, ffprobe_error = FFprobeService.runtime_check(ffprobe)
        if not ffprobe_ready and ffprobe_error:
            reasons.append(ffprobe_error)
    x265_available = False
    if ffmpeg is not None:
        x265_available, x265_error = FFmpegEncoder.runtime_check(ffmpeg)
        if not x265_available and x265_error:
            reasons.append(x265_error)
    root = workspace_root.expanduser().absolute()
    writable = workspace_writable(root)
    if not writable:
        reasons.append(f"Workspace jobs directory is missing or not writable: {root / 'jobs'}")
    try:
        resolved = root.resolve()
        for media_root in media_roots.values():
            media = media_root.resolve()
            if resolved == media or resolved.is_relative_to(media) or media.is_relative_to(resolved):
                reasons.append("Workspace and media roots must not overlap.")
                break
    except OSError:
        reasons.append("Workspace or media root cannot be resolved.")
    return {"available": not reasons, "ffmpeg_available": ffmpeg is not None,
            "ffprobe_available": ffprobe_ready, "libx265_available": x265_available,
            "ffmpeg_binary": ffmpeg or ffmpeg_binary,
            "ffprobe_binary": ffprobe or ffprobe_binary, "workspace_root": str(root),
            "workspace_writable": writable, "unavailable_reason": "; ".join(reasons) if reasons else None}
