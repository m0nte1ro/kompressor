"""Side-by-side comparison screenshots from the command line.

    .venv/bin/python -m app.tools.compare SOURCE OUTPUT [--count 6] [--out DIR]

Uses the same ffmpeg command as the History "Compare" button: random frames
between 5% and 95% of the source, source on the left, output on the right,
written as PNGs to DIR (default /mnt/kompressor/compare/manual-<time>).
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.services.analysis import comparison_timestamps, screenshot_command
from app.services.ffprobe import FFprobeService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Side-by-side comparison screenshots (source left, output right).")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ffmpeg", default=settings.ffmpeg_binary)
    parser.add_argument("--ffprobe", default=settings.ffprobe_binary)
    args = parser.parse_args(argv)

    probe = FFprobeService(args.ffprobe).inspect(args.source)
    video = next((s for s in probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)
    if video is None or probe.duration_seconds is None:
        print("Source has no video stream or unknown duration.", file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = args.out or settings.workspace_root / "compare" / f"manual-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    for number, seconds in enumerate(comparison_timestamps(probe.duration_seconds, args.count), start=1):
        destination = folder / f"{number:02d}_{seconds:010.3f}s_source-left_output-right.png"
        print(f"[{number}/{args.count}] {seconds:.3f}s -> {destination}", flush=True)
        subprocess.run(screenshot_command(args.ffmpeg, args.source, args.output, video, seconds, destination),
                       check=True)
    print(f"Done: {folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
