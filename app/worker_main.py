"""Standalone real encoder process.

The web application owns HTTP/UI only. Real ffmpeg subprocesses are owned by this
process so restarting the WebUI cannot interrupt an active encode.
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys
from pathlib import Path
from typing import IO, Literal
from threading import Event

from app.config import settings
from app.container import build_media_processor
from app.models.preset import normalize_backend
from app.workers.real import RealEncoderWorker


# Another worker already owns the lane. systemd must not keep retrying it.
EXIT_LANE_OWNED = 4


def acquire_lane(database_path: Path, backend: str) -> IO[str] | None:
    """Hold an exclusive lock for this lane for the life of the process.

    Startup recovery requeues the lane's active job and deletes its workspace,
    so a second worker for the same lane (e.g. an old qsv unit next to the gpu
    unit) would destroy the first one's running encode. The kernel drops the
    lock when the process exits, however it exits.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    handle = (database_path.parent / f".kompressor-worker-{backend}.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip() or "unknown"
        handle.close()
        print(f"Another {backend.upper()} worker (pid {owner}) already owns this lane; exiting.", file=sys.stderr)
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def run(backend: str = "cpu") -> int:
    # "qsv" is the GPU worker's name before the rename; old service units still pass it.
    if backend == "qsv":
        print("Worker argument 'qsv' is deprecated; use 'gpu' (kompressor-worker-gpu.service).", file=sys.stderr)
    backend = normalize_backend(backend)
    if backend == "cpu":
        selected_backend: Literal["cpu", "gpu"] = "cpu"
    elif backend == "gpu":
        selected_backend = "gpu"
    else:
        print(f"Unknown real worker backend: {backend}", file=sys.stderr)
        return 2

    lane = acquire_lane(settings.database_path, selected_backend)
    if lane is None:
        return EXIT_LANE_OWNED
    processor = build_media_processor(settings, process_role="worker", worker_backend=selected_backend)
    worker = processor.queue.worker
    if not isinstance(worker, RealEncoderWorker):
        print("Real worker requires KOMPRESSOR_MEDIA_BACKEND=filesystem.", file=sys.stderr)
        return 2
    if backend not in worker.supported_backends or not worker.enabled:
        # Usually transient (the render device or driver is not ready yet after a
        # host/LXC boot): exit 3 so systemd retries, unlike configuration errors (2).
        print(worker.unavailable_reason or f"{backend.upper()} worker is unavailable in this runtime.", file=sys.stderr)
        return 3

    stopping = Event()

    def request_stop(*_args):
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    worker.start()
    try:
        while not stopping.wait(1.0):
            worker.wake()
    finally:
        worker.shutdown()
        if processor.discovery:
            processor.discovery.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else "cpu"))
