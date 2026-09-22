from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import PROJECT_ROOT


router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=PROJECT_ROOT / "app" / "templates")


def size(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1_000_000_000:.1f} GB"


def bitrate(value: int) -> str:
    return f"{value / 1_000_000:g} Mbps"


def label(value: str | None) -> str:
    labels = {"cpu": "CPU · x265", "qsv": "Intel QSV", "hevc": "HEVC",
              "h264": "H.264", "preserve": "Preserve source", "efficient": "Efficient E-AC3 / AAC",
              "max_1080p": "Max 1080p", "max_720p": "Max 720p", "hdr10": "HDR10",
              "dolby_vision": "Dolby Vision", "dolby_vision_hdr10": "DV + HDR10"}
    return labels.get(value, (value or "SDR").replace("_", " ").upper())


templates.env.filters.update(size=size, bitrate=bitrate, label=label)


def render(request: Request, template: str, page: str, title: str, **context):
    return templates.TemplateResponse(request=request, name=template, context={
        "page": page, "title": title, **context,
    })


def rows(request: Request, scope: str, show_id: str | None = None) -> list[dict]:
    catalog = request.app.state.catalog
    result = []
    for entry in catalog.entries():
        if entry.scope != scope or (show_id is not None and entry.show_id != show_id):
            continue
        try:
            preset, eligibility = catalog.preview(entry)
        except LookupError as error:
            raise HTTPException(503, str(error)) from error
        result.append({"entry": entry, "item": entry.item,
                       "preset": preset, "eligibility": eligibility})
    return result


@router.get("/")
def index():
    return RedirectResponse("/movies", status_code=307)


@router.get("/movies", response_class=HTMLResponse)
def movies(request: Request):
    return render(request, "library.html", "movies", "Movies", scope="movie",
                  rows=rows(request, "movie"), subtitle="Media inventory and compression policies")


@router.get("/shows", response_class=HTMLResponse)
def shows(request: Request):
    library = request.app.state.catalog.library()
    cards = []
    for show in library.shows:
        episodes = [e for season in show.seasons for e in season.episodes]
        cards.append({"show": show, "count": len(episodes), "size": sum(e.size for e in episodes)})
    return render(request, "shows.html", "shows", "Shows", cards=cards,
                  subtitle="Series inventory · inherited series, season and episode tags")


@router.get("/shows/{show_id}", response_class=HTMLResponse)
def episodes(request: Request, show_id: str):
    show = next((s for s in request.app.state.catalog.library().shows if s.id == show_id), None)
    if show is None:
        raise HTTPException(404, "Show not found.")
    return render(request, "library.html", "shows", show.name, scope="show", show=show,
                  rows=rows(request, "show", show_id), subtitle="All episodes · series → season → episode policy")


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    return render(request, "queue.html", "queue", "Queue",
                  subtitle="Independent CPU and Intel QSV workers · estimated saving first")


@router.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return render(request, "history.html", "history", "History",
                  subtitle="Completed and skipped simulations · no actual storage savings")


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return render(request, "settings.html", "settings", "Settings",
                  subtitle="Presets and persistent library preferences",
                  presets=request.app.state.catalog.presets.get_all())
