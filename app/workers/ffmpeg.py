"""Owned ffmpeg subprocesses and workspace-only output paths."""
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess
from threading import Lock, Thread
from time import monotonic

from app.models.probe import MediaProbeResult
from app.models.queue import QueueJob
from app.services.encoding_capability import SUPPORTED_PIXEL_FORMATS


class FFmpegError(RuntimeError):
    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code


class EncodingCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class FFmpegOutput:
    partial_path: Path
    final_path: Path
    exit_code: int
    stderr: str


class FFmpegEncoder:
    @staticmethod
    def runtime_check(binary: str) -> tuple[bool, str | None]:
        try:
            result = subprocess.run([binary, "-hide_banner", "-encoders"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"Could not inspect ffmpeg encoders: {error}"
        if result.returncode != 0:
            return False, f"ffmpeg -encoders exited with status {result.returncode}."
        if "libx265" not in result.stdout + result.stderr:
            return False, "ffmpeg does not report the libx265 encoder."
        return True, None

    def __init__(self, binary: str, workspace_root: Path, stop_grace_seconds: float = 5.0):
        self.binary = binary
        self.workspace_root = workspace_root.expanduser().absolute()
        self.stop_grace_seconds = stop_grace_seconds
        self._lock = Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._paths: dict[str, tuple[Path, Path]] = {}

    def paths(self, job: QueueJob, source_path: Path) -> tuple[Path, Path]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job.id):
            raise FFmpegError("Job ID is not a safe workspace directory name.")
        jobs_root = self.workspace_root / "jobs"
        if self.workspace_root.is_symlink() or jobs_root.is_symlink():
            raise FFmpegError("Workspace root and jobs directory cannot be symlinks.")
        jobs_root.mkdir(parents=True, exist_ok=True)
        if not jobs_root.is_dir() or not jobs_root.resolve().is_relative_to(self.workspace_root.resolve()):
            raise FFmpegError("Jobs directory escaped the configured workspace root.")
        job_dir = jobs_root / job.id
        if job_dir.is_symlink():
            raise FFmpegError("Job workspace cannot be a symlink.")
        created = not job_dir.exists()
        job_dir.mkdir(mode=0o700, exist_ok=True)
        resolved = job_dir.resolve()
        if not resolved.is_relative_to(jobs_root.resolve()):
            raise FFmpegError("Job workspace escaped the configured workspace root.")
        if created:
            os.chmod(job_dir, 0o700)
        elif job_dir.stat().st_mode & 0o077:
            raise FFmpegError("Existing job workspace permissions must be private (0700).")
        stem = source_path.stem[:160]
        partial = resolved / f"{stem}.kompressor.partial.mkv"
        final = resolved / f"{stem}.kompressor.mkv"
        with self._lock:
            self._paths[job.id] = (partial, final)
        return partial, final

    @staticmethod
    def build_command(binary: str, job: QueueJob, source_path: Path,
                      partial_path: Path, probe: MediaProbeResult) -> list[str]:
        primary = next((s for s in probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)
        if primary is None:
            raise FFmpegError("Source has no primary video stream.")
        if primary.pixel_format not in SUPPORTED_PIXEL_FORMATS:
            raise FFmpegError(f"Unsupported or unknown source pixel format: {primary.pixel_format or 'unknown'}.")
        streams = probe.streams if job.preserve_subtitles else [s for s in probe.streams if s.kind in {"video", "audio"}]
        ordered = [primary, *(s for s in streams if s.index != primary.index)]
        command = [binary, "-hide_banner", "-nostdin", "-y", "-v", "error",
                   "-progress", "pipe:1", "-nostats", "-protocol_whitelist", "file,pipe",
                   "-i", str(source_path)]
        for stream in ordered:
            command.extend(["-map", f"0:{stream.index}"])
        command.extend(["-map_metadata", "0" if job.preserve_subtitles else "-1",
                        "-map_chapters", "0" if job.preserve_subtitles else "-1",
                        "-c", "copy", "-c:v:0", "libx265"])
        if job.preset.rate_control == "crf" and job.preset.quality_value is not None:
            command.extend(["-crf:v:0", str(job.preset.quality_value)])
        elif job.preset.rate_control == "abr" and job.preset.target_video_bitrate is not None:
            command.extend(["-b:v:0", str(job.preset.target_video_bitrate)])
        else:
            raise FFmpegError(f"Unsupported video rate control: {job.preset.rate_control}.")
        command.extend(["-preset:v:0", job.preset.encoder_preset,
                        "-pix_fmt:v:0", "yuv420p10le" if job.preset.output_bit_depth == 10 else "yuv420p",
                        "-f", "matroska", str(partial_path)])
        return command

    def encode(self, job: QueueJob, source_path: Path, probe: MediaProbeResult,
               progress_callback: Callable[[float | None, float], None],
               before_start: Callable[[], None] | None = None) -> FFmpegOutput:
        partial, final = self.paths(job, source_path)
        partial.unlink(missing_ok=True)
        # A prior interrupted attempt can leave a final file in the same job directory.
        final.unlink(missing_ok=True)
        command = self.build_command(self.binary, job, source_path, partial, probe)
        started = monotonic()
        output_us = 0
        stderr_tail: deque[str] = deque()
        stderr_lock = Lock()
        process: subprocess.Popen[str] | None = None
        try:
            if before_start is not None:
                before_start()
            process = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            with self._lock:
                cancelled = job.id in self._cancelled
                if not cancelled:
                    self._processes[job.id] = process
            if cancelled:
                self._terminate(process)
                raise EncodingCancelled("Encoding was stopped before it started.")

            def read_stderr():
                assert process is not None and process.stderr is not None
                for line in process.stderr:
                    with stderr_lock:
                        stderr_tail.append(line.rstrip())
                        while sum(len(s) for s in stderr_tail) > 16_384 and stderr_tail:
                            stderr_tail.popleft()
            stderr_thread = Thread(target=read_stderr, name=f"ffmpeg-stderr-{job.id}", daemon=True)
            stderr_thread.start()
            assert process.stdout is not None
            for line in process.stdout:
                key, separator, value = line.strip().partition("=")
                if not separator:
                    continue
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        output_us = max(output_us, int(value))
                    except ValueError:
                        continue
                elif key == "progress":
                    duration = probe.duration_seconds
                    percent = min(99.0, output_us / (duration * 1_000_000) * 100) if duration and duration > 0 else None
                    progress_callback(percent, monotonic() - started)
            exit_code = process.wait()
            stderr_thread.join(timeout=1)
            with self._lock:
                cancelled = job.id in self._cancelled
            if cancelled:
                raise EncodingCancelled("Encoding was stopped.")
            with stderr_lock:
                error_text = "\n".join(stderr_tail)[-16_384:]
            if exit_code != 0:
                raise FFmpegError(error_text or f"ffmpeg exited with status {exit_code}.", exit_code)
            return FFmpegOutput(partial, final, exit_code, error_text)
        except FileNotFoundError as error:
            partial.unlink(missing_ok=True)
            raise FFmpegError(f"ffmpeg executable not found: {self.binary}") from error
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise FFmpegError(f"Could not run ffmpeg: {error}") from error
        finally:
            if process is not None and process.poll() is None:
                self._terminate(process)
            with self._lock:
                self._processes.pop(job.id, None)
                self._cancelled.discard(job.id)

    def promote(self, job_id: str, output: FFmpegOutput) -> Path:
        with self._lock:
            expected = self._paths.get(job_id)
        if expected is None or (output.partial_path, output.final_path) != expected:
            raise FFmpegError("Output paths do not belong to this encoder job.")
        jobs_root = self.workspace_root / "jobs"
        job_dir = jobs_root / job_id
        if (self.workspace_root.is_symlink() or jobs_root.is_symlink() or job_dir.is_symlink()
                or not job_dir.resolve().is_relative_to(jobs_root.resolve())
                or output.partial_path.parent.resolve() != job_dir.resolve()
                or output.final_path.parent.resolve() != job_dir.resolve()):
            raise FFmpegError("Output paths escaped the private job workspace.")
        if not output.partial_path.is_file() or output.partial_path.is_symlink():
            raise FFmpegError("Validated partial output is no longer a regular file.")
        os.replace(output.partial_path, output.final_path)
        return output.final_path

    def cleanup_job_directory(self, job_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            return
        jobs_root = self.workspace_root / "jobs"
        job_dir = jobs_root / job_id
        if (self.workspace_root.is_symlink() or jobs_root.is_symlink()
                or job_dir.is_symlink() or not job_dir.is_dir()):
            return
        if not job_dir.resolve().is_relative_to(jobs_root.resolve()):
            return
        for path in job_dir.iterdir():
            if path.name.endswith(".kompressor.partial.mkv") or path.name.endswith(".kompressor.mkv"):
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink(missing_ok=True)
                except OSError:
                    # A read-only/unmounted workspace must not prevent startup recovery.
                    continue

    def cleanup(self, job_id: str, remove_final: bool = False) -> None:
        with self._lock:
            paths = self._paths.get(job_id)
        if not paths:
            return
        partial, final = paths
        for path in (partial, final) if remove_final else (partial,):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Preserve job failure/skip state if the workspace turned read-only.
                continue

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            # It may have exited between poll() and terminate().
            pass
        try:
            process.wait(timeout=self.stop_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=2)

    def stop(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)
            process = self._processes.get(job_id)
        if process is not None:
            self._terminate(process)
        # A Stop & Skip removes only the unvalidated partial; a completed output
        # is never deleted as a side effect of a late stop request.
        self.cleanup(job_id)

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._processes)
        for job_id in jobs:
            self.stop(job_id)
