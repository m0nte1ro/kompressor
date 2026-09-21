from fastapi import APIRouter, HTTPException, Request, Response

from app.models.queue import EnqueueRequest, PriorityRequest
from app.services.queue import QueueConflict


router = APIRouter(prefix="/api/queue", tags=["Fake queue"])


def perform(request: Request, action: str, *args):
    try:
        return getattr(request.app.state.queue_service, action)(*args)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except QueueConflict as error:
        raise HTTPException(409, str(error)) from error


@router.get("")
def queue(request: Request) -> dict:
    return perform(request, "snapshot")


@router.post("", status_code=201)
def enqueue(request: Request, payload: EnqueueRequest) -> dict:
    return perform(request, "enqueue", payload)


@router.delete("/{job_id}", status_code=204)
def remove(request: Request, job_id: str) -> Response:
    perform(request, "remove", job_id)
    return Response(status_code=204)


@router.patch("/{job_id}/priority", status_code=204)
def priority(request: Request, job_id: str, payload: PriorityRequest) -> Response:
    perform(request, "prioritize", job_id, payload.priority)
    return Response(status_code=204)


@router.post("/{job_id}/move-next", status_code=204)
def move_next(request: Request, job_id: str) -> Response:
    perform(request, "prioritize", job_id)
    return Response(status_code=204)


@router.post("/{job_id}/skip", status_code=204)
def skip(request: Request, job_id: str) -> Response:
    perform(request, "skip", job_id)
    return Response(status_code=204)
