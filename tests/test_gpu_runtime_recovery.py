"""GPU lane runtime regressions (no physical GPU required)."""
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Lock
from time import monotonic, sleep

from app.workers.real import RealEncoderWorker
from app.workers.ffmpeg import FFmpegError


def bare_worker():
    """Exercise runtime state transitions without launching ffmpeg/threads."""
    worker = object.__new__(RealEncoderWorker)
    worker.supported_backends = frozenset({"cpu"})
    worker.enabled = True
    worker.unavailable_reason = "GPU unavailable"
    worker._availability_checked = 0.0
    worker._availability_lock = Lock()
    worker._availability_running = False
    worker.latest_runtime = None
    worker.availability_probe = None
    worker._gpu_degraded = False
    worker._gpu_retry_at = 0.0
    return worker


def wait_for_backend(worker, expected):
    deadline = monotonic() + 2.0
    while monotonic() < deadline:
        if worker.supported_backends == expected:
            return
        sleep(0.01)
    assert worker.supported_backends == expected


def test_web_runtime_refresh_tracks_gpu_recovery_and_loss():
    worker = bare_worker()
    # The initial request must not block on a smoke encode.
    started, release = Event(), Event()
    status = {"supported_backends": ["cpu", "gpu"], "unavailable_reason": None}
    def probe():
        started.set()
        assert release.wait(1.0)
        return status
    worker.availability_probe = probe
    worker.encoder = SimpleNamespace(
        gpu_device=Path("/dev/dri/renderD128"),
        gpu_device_check=lambda path: (False, None))
    assert worker.refresh_availability() == frozenset({"cpu"})
    assert started.wait(1.0)
    assert worker.refresh_availability() == frozenset({"cpu"})
    release.set()
    wait_for_backend(worker, frozenset({"cpu", "gpu"}))
    assert worker.unavailable_reason is None

    # Render node loss triggers the next asynchronous probe, not an HTTP
    # blocking probe. A fresh healthy device avoids repeated smoke tests.
    status = {"supported_backends": ["cpu"], "unavailable_reason": "GPU unavailable"}
    worker._availability_checked = 0.0
    # The background thread may finish before the request returns; both
    # snapshots are valid, but the final state must converge to CPU-only.
    assert worker.refresh_availability() in (
        frozenset({"cpu", "gpu"}), frozenset({"cpu"}))
    wait_for_backend(worker, frozenset({"cpu"}))
    assert worker.unavailable_reason == "GPU unavailable"


def test_gpu_device_loss_pauses_claims_until_a_real_smoke_test_recovers():
    worker = bare_worker()
    calls = []
    device = Path("/dev/dri/renderD128")
    usable = {"device": False, "driver": False}
    worker.encoder = SimpleNamespace(
        gpu_device=device, binary="ffmpeg", workspace_root=Path("/tmp"),
        gpu_device_check=lambda path: (usable["device"], None),
        vaapi_runtime_check=lambda *args: (calls.append(args) or usable["driver"], None),
    )
    worker.probe = SimpleNamespace(binary="ffprobe")
    assert not worker._gpu_usable()
    assert not calls
    usable["device"] = True
    worker._gpu_retry_at = 0.0
    assert not worker._gpu_usable()
    assert len(calls) == 1
    usable["driver"] = True
    worker._gpu_retry_at = 0.0
    assert worker._gpu_usable()
    assert not worker._gpu_degraded


def test_gpu_backend_failure_requeues_instead_of_marking_failed():
    worker = bare_worker()
    worker.encoder = SimpleNamespace(
        gpu_device=Path("/dev/dri/renderD128"), binary="ffmpeg",
        workspace_root=Path("/tmp"),
        vaapi_runtime_check=lambda *args: (False, "driver not ready"),
    )
    worker.probe = SimpleNamespace(binary="ffprobe")
    recorded = []
    worker.queue = SimpleNamespace(
        requeue_real_job=lambda job_id, reason: (recorded.append((job_id, reason)) or True))
    worker._persist = lambda what, action: action()
    job = SimpleNamespace(id="test-gpu", backend="gpu")
    assert worker._gpu_failed_after_error(job, FFmpegError("vaapi failed"))
    assert worker._gpu_degraded
    assert recorded[0][0] == "test-gpu"
    assert "waiting for recovery" in recorded[0][1]
