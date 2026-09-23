from fastapi import APIRouter, Response

from app.dependencies import Processor
from app.models.queue import EnqueueRequest, PriorityRequest


router = APIRouter(prefix="/api/queue", tags=["Queue"])


@router.get("")
def queue(processor: Processor) -> dict:
    return processor.get_queue()


@router.post("", status_code=201)
def enqueue(processor: Processor, payload: EnqueueRequest) -> dict:
    return processor.queue_encode(payload)


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
