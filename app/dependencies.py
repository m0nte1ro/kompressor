"""HTTP dependency and application-error mapping; no service construction here."""
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.services.errors import Conflict, InvalidOperation, NotFound
from app.services.media_processor import MediaProcessor


def get_media_processor(request: Request) -> MediaProcessor:
    return request.app.state.media_processor


Processor = Annotated[MediaProcessor, Depends(get_media_processor)]


def register_error_handlers(application: FastAPI) -> None:
    async def not_found(request, error):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    async def conflict(request, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    async def invalid(request, error):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    application.add_exception_handler(NotFound, not_found)
    application.add_exception_handler(Conflict, conflict)
    application.add_exception_handler(InvalidOperation, invalid)
