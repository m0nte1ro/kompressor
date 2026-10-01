"""Startup checks for real CPU and Intel GPU (VA-API) encoder capabilities."""
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
                      gpu_device: Path = Path("/dev/dri/renderD128"),
                      backends: set[str] | frozenset[str] = frozenset({"cpu", "gpu"})) -> dict:
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

    requested = frozenset(backends)
    unknown = requested - {"cpu", "gpu"}
    if unknown:
        raise ValueError(f"Unknown encoder backends: {', '.join(sorted(unknown))}")

    x265_available: bool | None = None
    cpu_reasons: list[str] = []
    cpu_ready = False
    if "cpu" in requested:
        cpu_reasons = [*common_reasons]
        cpu_error = None
        if ffmpeg is not None:
            x265_available, cpu_error = FFmpegEncoder.runtime_check(ffmpeg)
        else:
            x265_available = False
        if not x265_available:
            cpu_reasons.append(cpu_error or "libx265 is unavailable.")
        cpu_ready = not cpu_reasons

    gpu_available: bool | None = None
    gpu_reasons: list[str] = []
    gpu_path = gpu_device.expanduser().absolute()
    gpu_ready = False
    if "gpu" in requested:
        gpu_reasons = [*common_reasons]
        gpu_error = None
        if ffmpeg is not None and ffprobe is not None and not common_reasons:
            gpu_available, gpu_error = FFmpegEncoder.vaapi_runtime_check(ffmpeg, gpu_path, ffprobe, root)
        else:
            gpu_available = False
        if not gpu_available:
            gpu_reasons.append(gpu_error or "GPU HEVC encoder (Intel VA-API) is unavailable.")
        gpu_ready = not gpu_reasons

    supported = [
        backend for backend, ready in (("cpu", cpu_ready), ("gpu", gpu_ready))
        if backend in requested and ready
    ]
    requested_reasons = [
        *(cpu_reasons if "cpu" in requested else []),
        *(gpu_reasons if "gpu" in requested else []),
    ]
    overall_reason = None if supported else "; ".join(dict.fromkeys(requested_reasons))

    return {
        "available": bool(supported),
        "cpu_available": cpu_ready,
        "gpu_available": gpu_ready,
        "supported_backends": supported,
        "ffmpeg_available": ffmpeg is not None,
        "ffprobe_available": ffprobe_ready,
        "libx265_available": x265_available,
        "hevc_vaapi_available": gpu_available,
        "gpu_device": str(gpu_path),
        "ffmpeg_binary": ffmpeg or ffmpeg_binary,
        "ffprobe_binary": ffprobe or ffprobe_binary,
        "workspace_root": str(root),
        "workspace_writable": writable,
        "cpu_unavailable_reason": "; ".join(dict.fromkeys(cpu_reasons)) if cpu_reasons else None,
        "gpu_unavailable_reason": "; ".join(dict.fromkeys(gpu_reasons)) if gpu_reasons else None,
        "unavailable_reason": overall_reason,
    }
