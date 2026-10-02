"""Owned ffmpeg subprocesses and workspace-only output paths."""
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import logging
import os
import re
import stat
import subprocess
from threading import Event, Lock, Thread
from time import monotonic

from app.workers import vaapi
from app.models.probe import MediaProbeResult, StreamFacts, assumed_sdr_colours, mapped_kinds
from app.models.queue import QueueJob
from app.services.encoding_capability import SUPPORTED_PIXEL_FORMATS
from app.services.estimation import plan_audio_tracks

log = logging.getLogger(__name__)


# mkvmerge track statistics describe the source bitstream; they are stale once a
# stream is re-encoded. Keys may carry a language suffix, e.g. BPS-eng.
MKV_STATISTICS_TAGS = {"BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
                       "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS"}


def stale_statistics_args(stream: StreamFacts, specifier: str) -> list[str]:
    """Empty -metadata values delete the copied key from the re-encoded output stream."""
    args = []
    for key in stream.metadata:
        base, separator, suffix = key.rpartition("-")
        name = base if separator and re.fullmatch(r"[A-Za-z]{2,3}", suffix) else key
        if name.upper() in MKV_STATISTICS_TAGS:
            args.extend([f"-metadata:s:{specifier}", f"{key}="])
    return args


def source_frame_count(stream: StreamFacts | None) -> int | None:
    """Frame count from the source's mkvmerge track statistics, if it has them."""
    if stream is None:
        return None
    for key, value in stream.metadata.items():
        base, separator, suffix = key.rpartition("-")
        name = base if separator and re.fullmatch(r"[A-Za-z]{2,3}", suffix) else key
        if name.upper() == "NUMBER_OF_FRAMES":
            try:
                count = int(str(value).strip())
            except ValueError:
                return None
            return count if count > 0 else None
    return None


class _StallWatch:
    """Reads ffmpeg's -progress output and kills the process once it stops advancing.

    ffmpeg keeps printing progress blocks while hung (a wedged GPU, a stalled
    mount), so only an advancing output position or frame count counts.
    """

    def __init__(self, terminate: Callable[[], None], seconds: float, name: str):
        self.seconds = seconds
        self.output_us = 0
        self.frames: int | None = None
        self.stalled = Event()
        self._terminate = terminate
        self._finished = Event()
        self._advanced = monotonic()
        Thread(target=self._watch, name=name, daemon=True).start()

    def _watch(self) -> None:
        while not self._finished.wait(min(5.0, self.seconds)):
            if monotonic() - self._advanced > self.seconds:
                self.stalled.set()
                self._terminate()
                return

    def feed(self, line: str) -> str | None:
        """Take one stdout line; returns its progress key, None if it has none."""
        key, separator, value = line.strip().partition("=")
        if not separator:
            return None
        if key in {"out_time_us", "out_time_ms"}:
            try:
                position = int(value)
            except ValueError:
                return key
            if position > self.output_us:
                self.output_us, self._advanced = position, monotonic()
        elif key == "frame":
            try:
                count = int(value)
            except ValueError:
                return key
            if self.frames is None or count > self.frames:
                self.frames, self._advanced = count, monotonic()
        return key

    def finish(self) -> None:
        self._finished.set()


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
    # Video frames ffmpeg reported writing; None when its progress never said.
    frames: int | None = None


class FFmpegEncoder:
    @staticmethod
    def _encoders(binary: str) -> tuple[str | None, str | None]:
        try:
            result = subprocess.run([binary, "-hide_banner", "-encoders"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, f"Could not inspect ffmpeg encoders: {error}"
        if result.returncode != 0:
            return None, f"ffmpeg -encoders exited with status {result.returncode}."
        return result.stdout + result.stderr, None

    @classmethod
    def runtime_check(cls, binary: str) -> tuple[bool, str | None]:
        encoders, error = cls._encoders(binary)
        if error:
            return False, error
        if "libx265" not in (encoders or ""):
            return False, "ffmpeg does not report the libx265 encoder."
        return True, None

    @staticmethod
    def gpu_device_check(device: Path) -> tuple[bool, str | None]:
        try:
            mode = device.stat().st_mode
        except OSError as error:
            return False, f"GPU render device is unavailable: {device} ({error})"
        if not stat.S_ISCHR(mode):
            return False, f"GPU render device is not a character device: {device}"
        if not os.access(device, os.R_OK | os.W_OK):
            return False, f"GPU render device is not readable/writable: {device}"
        return True, None

    @classmethod
    def vaapi_runtime_check(cls, binary: str, device: Path, ffprobe: str,
                            workspace: Path) -> tuple[bool, str | None]:
        ready, device_error = cls.gpu_device_check(device)
        if not ready:
            return False, device_error
        encoders, error = cls._encoders(binary)
        if error:
            return False, error
        if "hevc_vaapi" not in (encoders or ""):
            return False, "ffmpeg does not report the hevc_vaapi encoder."
        return vaapi.smoke_check(binary, device, ffprobe, workspace)

    def __init__(self, binary: str, workspace_root: Path, stop_grace_seconds: float = 5.0,
                 gpu_device: Path | None = None, stall_seconds: float = 900.0):
        self.binary = binary
        self.workspace_root = workspace_root.expanduser().absolute()
        self.stop_grace_seconds = stop_grace_seconds
        # An encode or verification run whose progress does not advance for this
        # long is hung (e.g. a wedged GPU) and is killed, so the lane can move on.
        self.stall_seconds = stall_seconds
        self.gpu_device = gpu_device.expanduser().absolute() if gpu_device else None
        self._lock = Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()
        self._paths: dict[str, tuple[Path, Path]] = {}

    def kept_output(self, job_id: str, path: str) -> Path:
        """A completed keep-original output, confirmed to be that job's own workspace file."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            raise FFmpegError("Job ID is not a safe workspace directory name.")
        jobs_root = self.workspace_root / "jobs"
        job_dir = jobs_root / job_id
        candidate = Path(path)
        if (self.workspace_root.is_symlink() or jobs_root.is_symlink() or job_dir.is_symlink()
                or candidate.is_symlink() or not candidate.name.endswith(".kompressor.mkv")
                or candidate.name.endswith(".kompressor.partial.mkv")
                or candidate.parent.resolve() != job_dir.resolve()
                or not job_dir.resolve().is_relative_to(jobs_root.resolve())):
            raise FFmpegError("The kept output is not a Kompressor workspace output of that job.")
        if not candidate.is_file():
            raise FFmpegError("The kept output is no longer in the workspace.")
        return candidate

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
                      partial_path: Path, probe: MediaProbeResult,
                      gpu_device: Path | None = None) -> list[str]:
        primary = next((s for s in probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)
        if primary is None:
            raise FFmpegError("Source has no primary video stream.")
        if primary.pixel_format not in SUPPORTED_PIXEL_FORMATS:
            raise FFmpegError(f"Unsupported or unknown source pixel format: {primary.pixel_format or 'unknown'}.")
        kinds = mapped_kinds(job.preserve_subtitles)
        streams = [s for s in probe.streams if s.kind in kinds]
        ordered = [primary, *(s for s in streams if s.index != primary.index)]

        command = [binary, "-hide_banner", "-nostdin", "-y", "-v", "error",
                   "-progress", "pipe:1", "-nostats", "-protocol_whitelist", "file,pipe"]
        if job.backend == "gpu":
            if gpu_device is None:
                raise FFmpegError("GPU job has no configured render device.")
            command.extend(vaapi.device_args(gpu_device))
            if vaapi.hardware_decode(primary):
                # Input options target the primary's actual index, not cover art.
                command.extend([f"-hwaccel:{primary.index}", "vaapi",
                                f"-hwaccel_device:{primary.index}", "va",
                                f"-hwaccel_output_format:{primary.index}", "vaapi"])
        command.extend(["-i", str(source_path)])

        for stream in ordered:
            command.extend(["-map", f"0:{stream.index}"])
        # A bare "-map_metadata -1" also drops per-stream tags, wiping every audio
        # track's language and title. Opting out of metadata only drops the global
        # container tags; stream tags always follow their stream.
        command.extend(["-map_metadata", "0"] if job.preserve_subtitles else ["-map_metadata:g", "-1"])
        command.extend(["-map_chapters", "0" if job.preserve_subtitles else "-1", "-c", "copy"])

        assumed = assumed_sdr_colours(primary)
        # ffmpeg 7.1 takes primaries/transfer from the frames and negotiates
        # -colorspace in the filtergraph. Untagged frames therefore lost the tags
        # and got a slow BT.601->BT.709 swscale matrix conversion. Stamping the
        # assumed SDR values on the frames avoids both.
        tag_frames = None
        if assumed is not None:
            tag_frames = "setparams=color_primaries={}:color_trc={}:colorspace={}".format(*assumed)

        if job.backend == "cpu":
            command.extend(["-c:v:0", "libx265"])
            if tag_frames is not None:
                pixel = "yuv420p10le" if job.preset.output_bit_depth == 10 else "yuv420p"
                command.extend(["-filter:v:0", f"format={pixel},{tag_frames}"])
            if job.preset.rate_control == "crf" and job.preset.quality_value is not None:
                command.extend(["-crf:v:0", str(job.preset.quality_value)])
            elif job.preset.rate_control == "abr" and job.preset.target_video_bitrate is not None:
                command.extend(["-b:v:0", str(job.preset.target_video_bitrate)])
            else:
                raise FFmpegError(f"Unsupported CPU video rate control: {job.preset.rate_control}.")
            command.extend(["-preset:v:0", job.preset.encoder_preset,
                            "-pix_fmt:v:0", "yuv420p10le" if job.preset.output_bit_depth == 10 else "yuv420p"])
        elif job.backend == "gpu":
            source_resolution = f"{primary.resolution_class}p" if primary.resolution_class else "unknown"
            try:
                nominal_bitrate = job.chosen_video_bitrate or job.preset.video_bitrate_for(source_resolution)
                command.extend(vaapi.video_args(
                    job.preset.output_bit_depth, job.preset.rate_control,
                    job.preset.quality_value, nominal_bitrate,
                    hardware=vaapi.hardware_decode(primary), tag_frames=tag_frames))
            except ValueError as error:
                raise FFmpegError(f"{error} Source resolution: {source_resolution}.") from error
        else:
            raise FFmpegError(f"Unsupported encoder backend: {job.backend}.")

        colours = assumed
        if colours is None and primary.hdr is not None:
            colours = primary.hdr.primaries, primary.hdr.transfer, primary.hdr.matrix
        if colours is not None:
            for flag, value in zip(("-color_primaries:v:0", "-color_trc:v:0", "-colorspace:v:0"), colours):
                if value:
                    command.extend([flag, value])
        if primary.color_range in {"tv", "pc"}:
            command.extend(["-color_range:v:0", primary.color_range])
        command.extend(stale_statistics_args(primary, "v:0"))

        subtitles = [stream for stream in ordered if stream.kind == "subtitle"]
        for output_index, stream in enumerate(subtitles):
            if stream.codec.lower() == "mov_text":
                command.extend([f"-c:s:{output_index}", "srt"])
        # "-c copy" above already copies every audio track bit-for-bit. Only tracks
        # the shared plan marks "encode" get codec options; nothing else touches audio.
        audio = [stream for stream in ordered if stream.kind == "audio"]
        for output_index, (stream, track) in enumerate(
                zip(audio, plan_audio_tracks(audio, job.preset, job.preserve_audio))):
            if track["action"] != "encode":
                continue
            command.extend([f"-c:a:{output_index}", str(track["codec"]),
                            f"-b:a:{output_index}", str(track["bitrate"]),
                            f"-ac:a:{output_index}", str(track["channels"])])
            command.extend(stale_statistics_args(stream, f"a:{output_index}"))

        command.extend(["-f", "matroska", str(partial_path)])
        return command

    def packet_hashes(self, job_id: str, path: Path, selectors: list[str]) -> list[str]:
        """SHA-256 of each selected stream's packet data, read without decoding.

        A stream-copied track produces identical packets, so equal hashes prove the
        output carries the source track bit-for-bit. The process is owned like an
        encode, so Stop & Skip and worker shutdown terminate it.
        """
        command = [self.binary, "-hide_banner", "-nostdin", "-v", "error",
                   "-protocol_whitelist", "file,pipe", "-i", str(path)]
        for selector in selectors:
            command.extend(["-map", selector])
        # Progress shares stdout with the hash report; the parser below skips it.
        command.extend(["-c", "copy", "-progress", "pipe:1", "-nostats",
                        "-f", "streamhash", "-hash", "sha256", "-"])
        stdout, stderr, returncode = self._run_owned(
            job_id, command, "verify audio", "Audio verification was stopped.")
        if returncode:
            raise FFmpegError(f"Audio verification failed: {stderr.strip()[-2000:]}", returncode)
        hashes: dict[int, str] = {}
        for line in stdout.splitlines():
            match = re.fullmatch(r"(\d+),a,SHA256=([0-9a-f]{64})", line.strip())
            if match:
                hashes[int(match[1])] = match[2]
        if sorted(hashes) != list(range(len(selectors))):
            raise FFmpegError("Audio verification returned an unexpected hash report.")
        return [hashes[index] for index in range(len(selectors))]

    def _run_owned(self, job_id: str, command: list[str], what: str,
                   stopped: str) -> tuple[str, str, int]:
        """Run a helper ffmpeg owned like an encode: Stop & Skip, shutdown and the
        stall watchdog end it. The command must write -progress to stdout."""
        try:
            process = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            raise FFmpegError(f"Could not run ffmpeg to {what}: {error}") from error
        with self._lock:
            cancelled = job_id in self._cancelled
            if not cancelled:
                self._processes[job_id] = process
        if cancelled:
            self._terminate(process)
            raise EncodingCancelled(stopped)
        assert process.stdout is not None and process.stderr is not None
        stdout: list[str] = []
        stderr: list[str] = []
        errors = process.stderr
        reader = Thread(target=lambda: stderr.extend(errors), name=f"ffmpeg-stderr-{job_id}", daemon=True)
        reader.start()
        watch = _StallWatch(lambda: self._terminate(process), self.stall_seconds, f"ffmpeg-watchdog-{job_id}")
        try:
            for line in process.stdout:
                stdout.append(line)
                watch.feed(line)
            process.wait()
            reader.join()
        finally:
            watch.finish()
            if process.poll() is None:
                self._terminate(process)
            with self._lock:
                self._processes.pop(job_id, None)
                cancelled = job_id in self._cancelled
        if cancelled:
            raise EncodingCancelled(stopped)
        if watch.stalled.is_set():
            raise FFmpegError(f"ffmpeg made no progress for {self.stall_seconds / 60:g} minutes "
                              f"while trying to {what}, and was stopped.", process.returncode)
        return "".join(stdout), "".join(stderr), process.returncode

    def decode_errors(self, job_id: str, path: Path, *, encoded_frames: int | None = None,
                      source_frames: int | None = None) -> list[str]:
        """Decode the output's video in full before anything trusts it.

        ffprobe validation reads headers and the first frames only, so damage
        further in (dropped or corrupt frames while ffmpeg still exits 0) would
        otherwise reach the source path. Any decoder error fails, and so does a
        frame count that differs from what the encoder wrote, or clearly from the
        source's own track statistics.
        """
        command = [self.binary, "-hide_banner", "-nostdin", "-v", "error",
                   "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0",
                   "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
        stdout, stderr, returncode = self._run_owned(
            job_id, command, "decode the output", "Output decoding was stopped.")
        problems = [line for line in stderr.strip().splitlines() if line.strip()]
        if returncode:
            detail = "; ".join(problems[:5]) or f"ffmpeg exited with status {returncode}"
            return [f"The output could not be decoded in full: {detail}"]
        if problems:
            more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
            return [f"Decoding the output reported errors: {'; '.join(problems[:5])}{more}"]
        frames = None
        for line in stdout.splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key == "frame":
                try:
                    frames = int(value)
                except ValueError:
                    continue
        if not frames:
            return ["Decoding the output produced no video frames."]
        errors = []
        if encoded_frames is not None and frames != encoded_frames:
            errors.append(f"The output decodes to {frames} video frames; the encoder wrote {encoded_frames}.")
        # mkvmerge statistics can be slightly off for remuxed sources; only a clear
        # difference (above 0.1%) means frames were lost while encoding.
        if source_frames is not None and abs(frames - source_frames) > max(2, source_frames // 1000):
            errors.append(f"The output has {frames} video frames; the source's track statistics "
                          f"list {source_frames}.")
        return errors

    def copied_audio_mismatches(self, job: QueueJob, source_path: Path, source_probe: MediaProbeResult,
                                output_path: Path, output_probe: MediaProbeResult) -> list[str]:
        """Prove every audio track planned as "copy" is bit-identical in the output."""
        source_audio = [stream for stream in source_probe.streams if stream.kind == "audio"]
        output_audio = [stream for stream in output_probe.streams if stream.kind == "audio"]
        if len(source_audio) != len(output_audio):
            return [f"Output has {len(output_audio)} audio tracks; expected {len(source_audio)}."]
        plan = plan_audio_tracks(source_audio, job.preset, job.preserve_audio)
        copied = [ordinal for ordinal, track in enumerate(plan) if track["action"] == "copy"]
        if not copied:
            return []
        source_hashes = self.packet_hashes(job.id, source_path, [f"0:{source_audio[i].index}" for i in copied])
        # Output audio keeps source order, so ordinal N is the Nth source audio track.
        output_hashes = self.packet_hashes(job.id, output_path, [f"0:a:{i}" for i in copied])
        errors = []
        for ordinal, expected, actual in zip(copied, source_hashes, output_hashes):
            if expected != actual:
                track = source_audio[ordinal]
                errors.append(f"Copied audio track {ordinal + 1} ({track.codec}"
                              f"{', ' + track.language if track.language else ''}) is not bit-identical to the source.")
        return errors

    def encode(self, job: QueueJob, source_path: Path, probe: MediaProbeResult,
               progress_callback: Callable[[float | None, float], None],
               before_start: Callable[[], None] | None = None) -> FFmpegOutput:
        partial, final = self.paths(job, source_path)
        partial.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        command = self.build_command(
            self.binary, job, source_path, partial, probe,
            gpu_device=self.gpu_device if job.backend == "gpu" else None,
        )
        started = monotonic()
        stderr_tail: deque[str] = deque()
        stderr_lock = Lock()
        process: subprocess.Popen[str] | None = None
        watch: _StallWatch | None = None
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
            owned = process
            watch = _StallWatch(lambda: self._terminate(owned), self.stall_seconds, f"ffmpeg-watchdog-{job.id}")
            assert process.stdout is not None
            for line in process.stdout:
                if watch.feed(line) == "progress":
                    duration = probe.duration_seconds
                    percent = (min(99.0, watch.output_us / (duration * 1_000_000) * 100)
                               if duration and duration > 0 else None)
                    progress_callback(percent, monotonic() - started)
            exit_code = process.wait()
            stderr_thread.join(timeout=1)
            with self._lock:
                cancelled = job.id in self._cancelled
            if cancelled:
                raise EncodingCancelled("Encoding was stopped.")
            if watch.stalled.is_set():
                raise FFmpegError(f"ffmpeg made no progress for {self.stall_seconds / 60:g} minutes "
                                  "and was stopped.", exit_code)
            with stderr_lock:
                error_text = "\n".join(stderr_tail)[-16_384:]
            if exit_code != 0:
                raise FFmpegError(error_text or f"ffmpeg exited with status {exit_code}.", exit_code)
            return FFmpegOutput(partial, final, exit_code, error_text, watch.frames)
        except FileNotFoundError as error:
            partial.unlink(missing_ok=True)
            raise FFmpegError(f"ffmpeg executable not found: {self.binary}") from error
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise FFmpegError(f"Could not run ffmpeg: {error}") from error
        finally:
            if watch is not None:
                watch.finish()
            if process is not None and process.poll() is None:
                self._terminate(process)
            with self._lock:
                self._processes.pop(job.id, None)

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
                    continue

    def cleanup(self, job_id: str, remove_final: bool = False) -> None:
        """End of a job: forget its stop request and remove its outputs."""
        with self._lock:
            # A stop that arrived after encode() (e.g. during audio verification)
            # must not linger into a later job with the same ID.
            self._cancelled.discard(job_id)
        self._remove_outputs(job_id, remove_final)

    def _remove_outputs(self, job_id: str, remove_final: bool = False) -> None:
        with self._lock:
            paths = self._paths.get(job_id)
        if not paths:
            return
        partial, final = paths
        for path in (partial, final) if remove_final else (partial,):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=self.stop_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Stuck in uninterruptible I/O (e.g. a hung mount); it exits when
                # the kernel lets it. Raising here would skip the caller's cleanup.
                log.error("ffmpeg (pid %s) has not exited 2 s after SIGKILL.", process.pid)

    def stop(self, job_id: str) -> None:
        with self._lock:
            self._cancelled.add(job_id)
            process = self._processes.get(job_id)
        if process is not None:
            self._terminate(process)
        # The stop request stays set until the job's own cleanup(): the encode
        # thread checks it after ffmpeg exits, and clearing it here raced that
        # check, so a stopped encode's exit status 255 was recorded as a failure.
        self._remove_outputs(job_id)

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._processes)
        for job_id in jobs:
            self.stop(job_id)
