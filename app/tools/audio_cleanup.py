"""Lossless MKV audio-track cleanup, opt-in standalone maintenance command.

Run only with Kompressor workers stopped. No queue/preset integration is implied.
A successful replacement intentionally leaves an untouched backup beside the file.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


class CleanupError(RuntimeError):
    pass


PORTUGUESE = {"pt", "por", "pt-pt", "pt-br"}
READ_ONLY_FLAGS = getattr(os, "ST_RDONLY", 1)


def run(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CleanupError(str(error)) from error
    if result.returncode:
        raise CleanupError(f"{args[0]} failed ({result.returncode}): {result.stderr[-1500:]}")
    return result


def probe(path: Path, ffprobe: str = "ffprobe") -> dict:
    return json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_chapters",
                           "-of", "json", str(path)], timeout=120).stdout)


def language(stream: dict) -> str:
    return str(stream.get("tags", {}).get("language", "und")).strip().lower().replace("_", "-")


def select_audio(info: dict, index: int | None) -> dict:
    tracks = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if index is not None:
        matches = [s for s in tracks if s["index"] == index]
        if not matches:
            raise CleanupError(f"Stream #{index} is not an audio track.")
        return matches[0]
    matches = [s for s in tracks if language(s) in PORTUGUESE]
    if len(matches) != 1:
        raise CleanupError(
            f"Expected exactly one Portuguese audio track; found {len(matches)}. "
            "Inspect the streams and specify --audio-index explicitly.")
    return matches[0]


def describe(info: dict) -> str:
    rows = []
    for stream in info["streams"]:
        if stream["codec_type"] == "audio":
            rows.append(f"#{stream['index']}: {stream.get('codec_name', '?')} "
                        f"{language(stream)} {stream.get('channels', '?')}ch "
                        f"{'[default]' if stream.get('disposition', {}).get('default') else ''}")
    return "\n".join(rows)


def identity(path: Path) -> tuple[int, int, int, int, int, int]:
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise CleanupError("Source must be a regular non-hardlinked file.")
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink


def streamhash(path: Path, index: int, ffmpeg: str) -> str:
    output = run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(path),
                  "-map", f"0:{index}", "-c", "copy", "-f", "streamhash",
                  "-hash", "SHA256", "-"], timeout=None).stdout
    lines = [line.strip() for line in output.splitlines() if "SHA256=" in line]
    if len(lines) != 1:
        raise CleanupError("Could not obtain exactly one stream hash.")
    return lines[0].split("SHA256=", 1)[1].strip().lower()


def check_output(source: Path, output: Path, src: dict, chosen: dict,
                 ffmpeg: str, ffprobe: str) -> None:
    dst = probe(output, ffprobe)
    source_video = [s for s in src["streams"] if s["codec_type"] == "video"]
    output_video = [s for s in dst["streams"] if s["codec_type"] == "video"]
    output_audio = [s for s in dst["streams"] if s["codec_type"] == "audio"]
    source_subs = [s for s in src["streams"] if s["codec_type"] == "subtitle"]
    output_subs = [s for s in dst["streams"] if s["codec_type"] == "subtitle"]
    source_attachments = [s for s in src["streams"] if s["codec_type"] == "attachment"]
    output_attachments = [s for s in dst["streams"] if s["codec_type"] == "attachment"]
    if (len(source_video) != len(output_video) or len(output_audio) != 1 or
        len(source_subs) != len(output_subs) or
        len(source_attachments) != len(output_attachments) or
        len(src.get("chapters", [])) != len(dst.get("chapters", []))):
        raise CleanupError("Remux changed the video, subtitle, attachment or chapter inventory.")
    if (output_audio[0].get("codec_name") != chosen.get("codec_name") or
        language(output_audio[0]) != language(chosen) or
        not output_audio[0].get("disposition", {}).get("default")):
        raise CleanupError("Selected audio codec/language/default disposition was not preserved.")
    pairs = [(old["index"], new["index"]) for old, new in zip(source_video, output_video)]
    pairs.append((chosen["index"], output_audio[0]["index"]))
    for old, new in pairs:
        if streamhash(source, old, ffmpeg) != streamhash(output, new, ffmpeg):
            raise CleanupError(f"Stream-copy hash mismatch for source stream #{old}.")


def cleanup(path: Path, *, audio_index: int | None = None,
            replace: bool = False, ffmpeg: str = "ffmpeg",
            ffprobe: str = "ffprobe") -> str:
    source = path.absolute()
    if source.suffix.lower() != ".mkv" or source.is_symlink():
        raise CleanupError("Only regular, non-symlink MKV files are supported.")
    before = identity(source)
    info = probe(source, ffprobe)
    chosen = select_audio(info, audio_index)
    if not any(s["codec_type"] == "video" for s in info["streams"]):
        raise CleanupError("Source has no video stream.")
    if os.statvfs(source.parent).f_flag & READ_ONLY_FLAGS:
        raise CleanupError("Source filesystem is read-only.")
    if not os.access(source.parent, os.W_OK | os.X_OK):
        raise CleanupError("Source directory is not writable.")
    if shutil.disk_usage(source.parent).free < before[2] + 256 * 1024 * 1024:
        raise CleanupError("Insufficient free space for a full-size staged remux.")

    unique = uuid4().hex
    staged = source.with_name(f".{source.stem}.{unique}.audio-cleanup.partial.mkv")
    backup = source.with_name(f".{source.stem}.{unique}.original.mkv")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source),
               "-map", "0:v", "-map", f"0:{chosen['index']}",
               "-map", "0:s?", "-map", "0:t?",
               "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
               "-disposition:a:0", "default", str(staged)]
    try:
        run(command)
        if identity(source) != before:
            raise CleanupError("Source changed while remuxing. Original left untouched.")
        check_output(source, staged, info, chosen, ffmpeg, ffprobe)
        if identity(source) != before:
            raise CleanupError("Source changed during verification. Original left untouched.")
        if not replace:
            final = source.with_name(f"{source.stem}.{unique}.audio-cleanup.mkv")
            staged.rename(final)
            return f"Verified output: {final} (source unchanged)"
        # Replace only after successful full stream-hash verification.
        source.rename(backup)
        try:
            staged.rename(source)
        except BaseException:
            backup.rename(source)
            raise
        return f"Replaced: {source}\nOriginal backup RETAINED: {backup}"
    finally:
        if staged.exists():
            staged.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep one PT audio track, copy video unchanged.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--audio-index", type=int, help="FFprobe stream index (required when multiple PT tracks)")
    parser.add_argument("--replace", action="store_true", help="Replace source, retaining a backup")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)
    try:
        info = probe(args.file, args.ffprobe)
        print("Audio tracks:\n" + describe(info), flush=True)
        selected = select_audio(info, args.audio_index)
        print(f"Keeping ONLY audio stream #{selected['index']} ({language(selected)}), default.", flush=True)
        print(cleanup(args.file, audio_index=args.audio_index, replace=args.replace,
                      ffmpeg=args.ffmpeg, ffprobe=args.ffprobe))
        return 0
    except (CleanupError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Audio cleanup refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
