"""Standalone real encoder process.

The web application owns HTTP/UI only. Real ffmpeg subprocesses are owned by this
process so restarting the WebUI cannot interrupt an active encode.
"""
from __future__ import annotations

import signal
import sys
from typing import Literal
from threading import Event

from app.config import settings
from app.container import build_media_processor
from app.workers.real import RealEncoderWorker


def run(backend: str = "cpu") -> int:
    if backend == "cpu":
        selected_backend: Literal["cpu", "qsv"] = "cpu"
    elif backend == "qsv":
        selected_backend = "qsv"
    else:
        print(f"Unknown real worker backend: {backend}", file=sys.stderr)
        return 2

    processor = build_media_processor(settings, process_role="worker", worker_backend=selected_backend)
    worker = processor.queue.worker
    if not isinstance(worker, RealEncoderWorker):
        print("Real worker requires KOMPRESSOR_MEDIA_BACKEND=filesystem.", file=sys.stderr)
        return 2
    if backend not in worker.supported_backends or not worker.enabled:
        # Usually transient (the render device or driver is not ready yet after a
        # host/LXC boot): exit 3 so systemd retries, unlike configuration errors (2).
        print(worker.unavailable_reason or f"{'GPU' if backend == 'qsv' else 'CPU'} worker is unavailable in this runtime.", file=sys.stderr)
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
