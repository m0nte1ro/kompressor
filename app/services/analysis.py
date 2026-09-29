"""Read-only quality tools for kept outputs: side-by-side screenshots and VMAF.

One ffmpeg task runs at a time in a background thread of the web process, at the
lowest CPU priority. Sources and outputs are only read; files are written only
below <workspace>/compare.

Frames are matched by position from each file's first video frame, not by
container time: a source whose container starts at -0.021 s (AAC priming) and
its output starting at 0 would otherwise be compared one frame apart, which
leaves screenshots subtly off and VMAF scores meaningless.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryFile
from threading import Event, Lock, Thread
import json
import os
import random
import re
import shutil
import subprocess

from app.models.probe import StreamFacts, assumed_sdr_colours
from app.services.errors import Conflict, InvalidOperation

BENCHMARK_SECONDS = (5, 600)


@dataclass(frozen=True)
class AnalysisTarget:
    job_id: str
    name: str
    source: Path
    output: Path
    duration: float
    video: StreamFacts  # primary source video stream


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")[:80] or "media"


def comparison_timestamps(duration: float, count: int, rng: random.Random | None = None) -> list[float]:
    """Random, sorted positions between 5% and 95% of the file (skips intros/credits edges)."""
    rng = rng or random.Random()
    low, high = duration * 0.05, duration * 0.95
    return sorted(round(rng.uniform(low, high), 3) for _ in range(count))


def low_priority(command: list[str]) -> list[str]:
    nice = shutil.which("nice")
    return [nice, "-n", "19", *command] if nice else command


def first_frame_command(ffmpeg: str, path: Path, selector: str) -> list[str]:
    """Decode one frame with absolute timestamps; showinfo logs its pts_time."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-protocol_whitelist", "file,pipe",
            "-copyts", "-i", str(path), "-map", selector, "-frames:v", "1",
            "-vf", "showinfo", "-f", "null", "-"]


def parse_first_frame(log: str) -> float:
    match = re.search(r"pts_time:(-?[0-9.]+)", log)
    if match is None:
        raise InvalidOperation("Could not read the first video frame time.")
    return float(match[1])


def half_frame(video: StreamFacts) -> float:
    try:
        rate = Fraction(video.frame_rate) if video.frame_rate else Fraction(25)
    except (ValueError, ZeroDivisionError):
        rate = Fraction(25)
    return float(0.5 / rate) if rate > 0 else 0.02


def seek(first_frame: float, seconds: float, video: StreamFacts) -> list[str]:
    """Absolute seek to `seconds` after a file's first frame, half a frame early so
    rounding cannot skip the intended frame in only one of the two files."""
    return ["-seek_timestamp", "1", "-ss", f"{first_frame + seconds - half_frame(video):.6f}"]


def screenshot_command(ffmpeg: str, source: Path, output: Path, video: StreamFacts,
                       source_seek: list[str], output_seek: list[str], destination: Path) -> list[str]:
    """One PNG: source frame on the left, the matching output frame on the right.

    Untagged sources the encoder assumed SDR for get the same colour tags stamped
    here, so both halves are converted to RGB with the same matrix.
    """
    if video.width is None or video.height is None:
        raise InvalidOperation("Source dimensions are unknown.")
    size = f"{video.width}:{video.height}"
    assumed = assumed_sdr_colours(video)
    tag = "setparams=color_primaries={}:color_trc={}:colorspace={},".format(*assumed) if assumed else ""
    graph = (f"[0:{video.index}]{tag}scale={size}:flags=bicubic,setsar=1,format=rgb24[source];"
             f"[1:v:0]scale={size}:flags=bicubic,setsar=1,format=rgb24[output];"
             "[source][output]hstack=inputs=2")
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
            *source_seek, "-i", str(source), *output_seek, "-i", str(output),
            "-filter_complex", graph, "-frames:v", "1", str(destination)]


def vmaf_command(ffmpeg: str, source: Path, output: Path, video: StreamFacts,
                 source_seek: list[str], output_seek: list[str], seconds: float,
                 log_path: Path, threads: int) -> list[str]:
    """libvmaf with the output as distorted input and the source as reference.

    Both are brought to 10-bit 4:2:0 (a lossless widening for 8-bit sources) so
    the filter compares like with like; no colour conversion happens.
    """
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", str(log_path)):
        raise InvalidOperation("VMAF log path contains characters that are unsafe in a filtergraph.")
    model = ":model='version=vmaf_4k_v0.6.1'" if (video.height or 0) > 1080 else ""
    graph = (f"[0:v:0]setpts=PTS-STARTPTS,format=yuv420p10le[distorted];"
             f"[1:{video.index}]setpts=PTS-STARTPTS,format=yuv420p10le[reference];"
             f"[distorted][reference]libvmaf=log_fmt=json:log_path='{log_path}':n_threads={threads}{model}")
    duration = f"{seconds:.3f}"
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-nostats", "-progress", "pipe:1",
            "-protocol_whitelist", "file,pipe",
            *output_seek, "-t", duration, "-i", str(output),
            *source_seek, "-t", duration, "-i", str(source),
            "-an", "-sn", "-dn", "-lavfi", graph, "-f", "null", "-"]


def summarize_vmaf(log: dict) -> dict:
    scores = sorted(frame["metrics"]["vmaf"] for frame in log.get("frames", []) if "vmaf" in frame.get("metrics", {}))
    if not scores:
        raise InvalidOperation("libvmaf reported no frames.")
    pooled = log.get("pooled_metrics", {}).get("vmaf", {})

    def percentile(fraction: float) -> float:
        return scores[min(len(scores) - 1, int(len(scores) * fraction))]

    return {"mean": round(pooled.get("mean", sum(scores) / len(scores)), 2),
            "harmonic_mean": round(pooled.get("harmonic_mean", 0.0), 2),
            "min": round(scores[0], 2), "p1": round(percentile(0.01), 2), "p5": round(percentile(0.05), 2),
            "frames": len(scores)}


class AnalysisService:
    def __init__(self, ffmpeg: str, compare_root: Path,
                 resolve: Callable[[str], AnalysisTarget],
                 record: Callable[[str, str, dict], None]):
        self.ffmpeg = ffmpeg
        self.compare_root = compare_root
        self.resolve = resolve
        self.record = record
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._process: subprocess.Popen | None = None
        self._busy = False
        self._vmaf_available: bool | None = None
        self._status: dict = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _update(self, **changes) -> None:
        with self._lock:
            self._status = {**self._status, **changes}

    def _begin(self, kind: str, job_id: str) -> AnalysisTarget:
        with self._lock:
            if self._busy:
                raise Conflict("Another comparison or benchmark is running; wait for it to finish.")
            target = self.resolve(job_id)
            self._busy = True
            self._status = {"state": "running", "kind": kind, "job_id": job_id, "name": target.name,
                            "progress": 0.0, "error": None, "result": None}
        return target

    def _launch(self, work: Callable[[], dict], kind: str, job_id: str) -> dict:
        def run():
            try:
                result = work()
                self.record(job_id, kind, result)
                self._update(state="completed", progress=100.0, result=result)
            except Exception as error:  # Reported to the History page; nothing else to undo.
                self._update(state="cancelled" if self._stop.is_set() else "failed", error=str(error))
            finally:
                with self._lock:
                    self._busy = False

        thread = Thread(target=run, name=f"kompressor-{kind}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return self.status()

    def _run(self, command: list[str],
             progress: Callable[[str, str], None] | None = None) -> subprocess.CompletedProcess:
        """Run ffmpeg at low priority; stderr goes to a file so a chatty run cannot block."""
        if self._stop.is_set():
            raise InvalidOperation("Analysis stopped.")
        with TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen(low_priority(command), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=errors, text=True)
            with self._lock:
                self._process = process
            lines = []
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    lines.append(line)
                    key, separator, value = line.strip().partition("=")
                    if progress and separator:
                        progress(key, value)
                process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                with self._lock:
                    self._process = None
            errors.seek(0)
            stderr = errors.read()
        if process.returncode:
            raise InvalidOperation(f"ffmpeg failed: {stderr.strip()[-1500:]}")
        return subprocess.CompletedProcess(command, process.returncode, "".join(lines), stderr)

    def _aligned_seeks(self, target: AnalysisTarget, seconds: float) -> tuple[list[str], list[str]]:
        source_first = parse_first_frame(self._run(first_frame_command(
            self.ffmpeg, target.source, f"0:{target.video.index}")).stderr)
        output_first = parse_first_frame(self._run(first_frame_command(
            self.ffmpeg, target.output, "0:v:0")).stderr)
        return seek(source_first, seconds, target.video), seek(output_first, seconds, target.video)

    def _run_folder(self, target: AnalysisTarget, kind: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        parent = self.compare_root / f"{slug(target.name)}-{target.job_id[:8]}"
        parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 100):
            folder = parent / (f"{kind}-{stamp}" if attempt == 1 else f"{kind}-{stamp}-{attempt}")
            try:
                folder.mkdir()
                return folder
            except FileExistsError:
                continue
        raise InvalidOperation("Could not create a new comparison folder.")

    def start_compare(self, job_id: str, count: int = 6) -> dict:
        if not 1 <= count <= 20:
            raise InvalidOperation("Choose between 1 and 20 screenshots.")
        target = self._begin("comparison", job_id)

        def work() -> dict:
            folder = self._run_folder(target, "compare")
            files = []
            stamps = comparison_timestamps(target.duration, count)
            for number, seconds in enumerate(stamps, start=1):
                clock = f"{int(seconds // 3600):02d}h{int(seconds % 3600 // 60):02d}m{seconds % 60:06.3f}s"
                destination = folder / f"{number:02d}_{clock}_source-left_output-right.png"
                source_seek, output_seek = self._aligned_seeks(target, seconds)
                self._run(screenshot_command(self.ffmpeg, target.source, target.output, target.video,
                                             source_seek, output_seek, destination))
                files.append(destination.name)
                self._update(progress=round(number / len(stamps) * 100, 1))
            return {"folder": str(folder), "files": files, "timestamps": stamps,
                    "created_at": datetime.now(timezone.utc).isoformat()}

        return self._launch(work, "comparison", job_id)

    def vmaf_available(self) -> bool:
        if self._vmaf_available is None:
            try:
                listing = subprocess.run([self.ffmpeg, "-hide_banner", "-filters"], stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=10).stdout
                self._vmaf_available = re.search(r"\blibvmaf\b", listing) is not None
            except (OSError, subprocess.TimeoutExpired):
                self._vmaf_available = False
        return self._vmaf_available

    def start_benchmark(self, job_id: str, seconds: float) -> dict:
        low, high = BENCHMARK_SECONDS
        if not low <= seconds <= high:
            raise InvalidOperation(f"Choose a benchmark duration between {low} and {high} seconds.")
        if not self.vmaf_available():
            raise InvalidOperation("This ffmpeg build has no libvmaf filter; install an ffmpeg with libvmaf.")
        target = self._begin("vmaf", job_id)

        def work() -> dict:
            length = min(seconds, target.duration)
            start = max(0.0, (target.duration - length) / 2)  # Centred: away from intros and credits.
            folder = self._run_folder(target, "vmaf")
            log_path = folder / "vmaf.json"
            source_seek, output_seek = self._aligned_seeks(target, start)

            def progress(key: str, value: str) -> None:
                if key == "out_time_us" and value.isdigit():
                    self._update(progress=round(min(99.0, int(value) / (length * 1e6) * 100), 1))

            threads = max(1, (os.cpu_count() or 2) // 2)
            self._run(vmaf_command(self.ffmpeg, target.source, target.output, target.video,
                                   source_seek, output_seek, length, log_path, threads), progress)
            summary = summarize_vmaf(json.loads(log_path.read_text()))
            return {**summary, "start_seconds": round(start, 3), "duration_seconds": round(length, 3),
                    "model": "vmaf_4k_v0.6.1" if (target.video.height or 0) > 1080 else "vmaf_v0.6.1",
                    "log": str(log_path), "created_at": datetime.now(timezone.utc).isoformat()}

        return self._launch(work, "vmaf", job_id)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            process, thread = self._process, self._thread
        if process is not None and process.poll() is None:
            process.terminate()
        if thread is not None:
            thread.join(timeout=10)
