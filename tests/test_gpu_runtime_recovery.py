"""GPU lane runtime regressions (no physical GPU required)."""
from pathlib import Path
from types import SimpleNamespace

from app.workers.real import RealEncoderWorker
from app.workers.ffmpeg import FFmpegError


def bare_worker():
    """Exercise runtime state transitions without launching ffmpeg/threads."""
    worker = object.__new__(RealEncoderWorker)
    worker.supported_backends = frozenset({"cpu"})
    worker.enabled = True
    worker.unavailable_reason = "GPU unavailable"
    worker._availability_checked = 0.0
    worker.availability_probe = None
    worker._gpu_degraded = False
    worker._gpu_retry_at = 0.0
    return worker


def test_web_runtime_refresh_tracks_gpu_recovery_and_loss():
    worker = bare_worker()
    status = {"supported_backends": ["cpu", "gpu"], "unavailable_reason": None}
    worker.availability_probe = lambda: status
    assert worker.refresh_availability() == frozenset({"cpu", "gpu"})
    assert worker.unavailable_reason is None
    # Requests inside the 30-second cache window cannot run repeated smoke tests.
    status = {"supported_backends": ["cpu"], "unavailable_reason": "GPU unavailable"}
    assert worker.refresh_availability() == frozenset({"cpu", "gpu"})
    worker._availability_checked = 0.0
    assert worker.refresh_availability() == frozenset({"cpu"})
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
