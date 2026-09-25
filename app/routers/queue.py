from typing import Literal

from fastapi import APIRouter, Response

from app.dependencies import Processor
from app.models.queue import EnqueueRequest, PriorityRequest
from app.models.preferences import WorkerSettings


router = APIRouter(prefix="/api/queue", tags=["Queue"])


@router.get("")
def queue(processor: Processor) -> dict:
    return processor.get_queue()


@router.post("", status_code=201)
def enqueue(processor: Processor, payload: EnqueueRequest) -> dict:
    return processor.queue_encode(payload)


@router.get("/workers")
def workers(processor: Processor) -> dict:
    return processor.get_worker_controls()


@router.put("/workers/settings")
def worker_settings(processor: Processor, payload: WorkerSettings) -> dict:
    return processor.update_worker_controls(payload)


@router.post("/workers/pause-all")
def pause_all(processor: Processor) -> dict:
    return processor.pause_all_workers()


@router.post("/workers/resume-all")
def resume_all(processor: Processor) -> dict:
    return processor.resume_all_workers()


@router.post("/workers/stop-all")
def stop_all(processor: Processor) -> dict:
    return processor.pause_all_workers(stop_active=True)


@router.post("/workers/{backend}/pause")
def pause_worker(processor: Processor, backend: Literal["cpu", "qsv"]) -> dict:
    return processor.pause_worker(backend)


@router.post("/workers/{backend}/resume")
def resume_worker(processor: Processor, backend: Literal["cpu", "qsv"]) -> dict:
    return processor.resume_worker(backend)


@router.post("/workers/{backend}/stop-active", status_code=202)
def stop_active_worker(processor: Processor, backend: Literal["cpu", "qsv"]) -> dict:
    return {"stopping": processor.stop_active_worker(backend)}


@router.delete("/{job_id}", status_code=204)
def remove(processor: Processor, job_id: str) -> Response:
    processor.remove_queued_job(job_id)
    return Response(status_code=204)


@router.patch("/{job_id}/priority", status_code=204)
def priority(processor: Processor, job_id: str, payload: PriorityRequest) -> Response:
    processor.prioritize_job(job_id, payload.priority)
    return Response(status_code=204)


@router.post("/{job_id}/move-next", status_code=204)
def move_next(processor: Processor, job_id: str) -> Response:
    processor.move_job_next(job_id)
    return Response(status_code=204)


@router.post("/{job_id}/skip", status_code=204)
def skip(processor: Processor, job_id: str) -> Response:
    processor.stop_job(job_id)
    return Response(status_code=204)
