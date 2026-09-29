"""Read-only quality tools for kept outputs: side-by-side screenshots (and VMAF).

One ffmpeg task runs at a time in a background thread of the web process, at the
lowest CPU priority. Sources and outputs are only read; files are written only
below <workspace>/compare.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
import random
import re
import shutil
import subprocess

from app.models.probe import StreamFacts, assumed_sdr_colours
from app.services.errors import Conflict, InvalidOperation


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


def screenshot_command(ffmpeg: str, source: Path, output: Path, video: StreamFacts,
                       seconds: float, destination: Path) -> list[str]:
    """One PNG: source frame on the left, output frame at the same time on the right.

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
    at = f"{seconds:.3f}"
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-protocol_whitelist", "file,pipe",
            "-ss", at, "-i", str(source), "-ss", at, "-i", str(output),
            "-filter_complex", graph, "-frames:v", "1", str(destination)]


def low_priority(command: list[str]) -> list[str]:
    nice = shutil.which("nice")
    return [nice, "-n", "19", *command] if nice else command


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

    def _run(self, command: list[str]) -> subprocess.CompletedProcess:
        if self._stop.is_set():
            raise InvalidOperation("Analysis stopped.")
        process = subprocess.Popen(low_priority(command), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with self._lock:
            self._process = process
        try:
            stdout, stderr = process.communicate()
        finally:
            with self._lock:
                self._process = None
        if process.returncode:
            raise InvalidOperation(f"ffmpeg failed: {(stderr or '').strip()[-1500:]}")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

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
                self._run(screenshot_command(self.ffmpeg, target.source, target.output,
                                             target.video, seconds, destination))
                files.append(destination.name)
                self._update(progress=round(number / len(stamps) * 100, 1))
            return {"folder": str(folder), "files": files, "timestamps": stamps,
                    "created_at": datetime.now(timezone.utc).isoformat()}

        return self._launch(work, "comparison", job_id)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            process, thread = self._process, self._thread
        if process is not None and process.poll() is None:
            process.terminate()
        if thread is not None:
            thread.join(timeout=10)
