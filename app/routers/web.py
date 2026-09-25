from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import PROJECT_ROOT
from app.dependencies import Processor


router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=PROJECT_ROOT / "app" / "templates")


def size(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / (1024 ** 3):.1f} GiB"


def bitrate(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1_000_000:.1f} Mbps"


def label(value: str | None) -> str:
    labels = {"cpu": "CPU · x265", "qsv": "Intel QSV", "hevc": "HEVC",
              "h264": "H.264", "preserve": "Preserve source", "keep": "Keep source resolution",
              "efficient": "Efficient E-AC3 / AAC", "max_2160p": "Max 2160p",
              "max_1080p": "Max 1080p", "max_720p": "Max 720p", "max_576p": "Max 576p",
              "max_480p": "Max 480p", "sdr_only": "SDR sources only",
              "hdr10_experimental": "SDR + HDR10 · experimental", "preserve_source": "Preserve source HDR mode",
              "tone_map_to_sdr": "Tone map HDR to SDR", "hdr10": "HDR10", "hdr10plus": "HDR10+", "unknown": "Unknown", "hlg": "HLG",
              "dolby_vision": "Dolby Vision", "dolby_vision_hdr10": "DV + HDR10"}
    return labels.get(value or "", (value or "SDR").replace("_", " ").upper())


templates.env.filters.update(size=size, bitrate=bitrate, label=label)


def render(request: Request, template: str, page: str, title: str, **context):
    return templates.TemplateResponse(request=request, name=template, context={
        "page": page, "title": title, **context,
    })


@router.get("/")
def index():
    return RedirectResponse("/movies", status_code=307)


@router.get("/movies", response_class=HTMLResponse)
def movies(request: Request, processor: Processor):
    return render(request, "library.html", "movies", "Movies", scope="movie",
                  rows=processor.get_movies(), subtitle="Media inventory and compression policies")


@router.get("/shows", response_class=HTMLResponse)
def shows(request: Request, processor: Processor):
    cards = processor.get_shows()
    return render(request, "shows.html", "shows", "Shows", cards=cards,
                  subtitle="Series inventory · inherited series, season and episode tags")


@router.get("/shows/{show_id}", response_class=HTMLResponse)
def episodes(request: Request, show_id: str, processor: Processor):
    detail = processor.get_show(show_id)
    return render(request, "library.html", "shows", detail["show"].name, scope="show", **detail,
                  subtitle="All episodes · series → season → episode policy")


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, processor: Processor):
    return render(request, "queue.html", "queue", "Queue",
                  subtitle="Independent CPU and Intel QSV workers · estimated saving first",
                  scan_status=processor.get_scan_status(), runtime_settings=processor.get_runtime_settings())


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, processor: Processor):
    return render(request, "history.html", "history", "History",
                  subtitle="Encode results and queue history · estimated and measured savings",
                  runtime_settings=processor.get_runtime_settings())


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request, processor: Processor):
    scan_status = processor.get_scan_status()
    if not isinstance(scan_status, dict):
        scan_status = {"backend": "seed", "state": "disabled", "roots": []}
    runtime_settings = processor.get_runtime_settings()
    if not isinstance(runtime_settings, dict):
        runtime_settings = {}
    runtime_settings = {
        "media_backend": scan_status.get("backend", "seed"),
        "ffmpeg_binary": "ffmpeg", "ffprobe_binary": "ffprobe",
        "ffmpeg_available": None, "ffprobe_available": None, "libx265_available": None,
        "workspace_root": "not configured", "workspace_writable": None,
        "encoding_enabled": False, "encoder_mode": "seed fake simulation",
        "supported_backends": ["cpu", "qsv"] if scan_status.get("backend") == "seed" else [],
        "unavailable_reason": None, **runtime_settings,
    }
    runtime_settings.setdefault("startup", dict(runtime_settings))
    return render(request, "settings.html", "settings", "Settings",
                  subtitle="Presets and persistent library preferences",
                  presets=processor.get_presets(), library_paths=processor.get_library_paths(),
                  worker_controls=processor.get_worker_controls(),
                  scan_status=scan_status, runtime_settings=runtime_settings)
