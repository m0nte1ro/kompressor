"""Narrow execution capability checks layered after the PolicyEngine."""
from app.models.media import Episode, Movie
from app.models.queue import QueueJob
from app.models.probe import StreamFacts
from app.services.estimation import audio_plan

MediaItem = Movie | Episode
SUPPORTED_PIXEL_FORMATS = {"yuv420p", "yuvj420p", "yuv420p10le", "yuv420p10be"}
SDR_TRANSFERS = {"bt709", "smpte170m", "bt470bg"}
SDR_PRIMARIES = {"bt709", "smpte170m", "bt470bg", "bt470m"}
SDR_MATRICES = {"bt709", "smpte170m", "bt470bg", "smpte240m", "fcc"}


def confirmed_sdr(stream: StreamFacts) -> bool:
    hdr = stream.hdr
    if hdr is None or hdr.classify().base != "sdr":
        return False
    if hdr.transfer not in SDR_TRANSFERS or hdr.primaries not in SDR_PRIMARIES:
        return False
    if hdr.matrix not in SDR_MATRICES:
        return False
    return not any((hdr.mastering_display, hdr.content_light, hdr.dolby_vision_config,
                    hdr.dolby_vision_rpu is True, hdr.hdr10plus_metadata is True))


class CPUEncodeCapability:
    """The first real worker supports only a conservative CPU/HEVC/SDR slice."""

    def reasons(self, item: MediaItem, job: QueueJob) -> list[str]:
        reasons = []
        if not item.processing_supported:
            reasons.append("Real encoding prerequisites are unavailable.")
        if job.backend != "cpu":
            reasons.append("Real encoding currently supports the CPU backend only.")
        if job.preset.destination_codec != "hevc":
            reasons.append("Real encoding currently supports HEVC output only.")
        if job.preset.target_resolution != "keep":
            reasons.append("Real encoding currently preserves the source resolution only.")
        if job.replace_source:
            reasons.append("Source replacement is disabled; keep-output jobs only.")
        if job.source_file_id is None or job.source_revision_id is None:
            reasons.append("A reconciled filesystem file and revision are required.")
        if job.source_reference is None:
            reasons.append("Captured source root/path/physical identity is required.")
        probe = item.probe
        if probe is None:
            reasons.append("Source technical metadata is unavailable.")
            return reasons
        primary = next((s for s in probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)
        if primary is None:
            reasons.append("Source has no primary video stream.")
            return reasons
        if primary.scan_type != "progressive" or item.interlaced is not False:
            reasons.append(f"Real encoding requires progressive video; source scan type is {primary.scan_type}.")
        if not confirmed_sdr(primary):
            reasons.append("Real encoding currently supports confirmed SDR sources only.")
        if item.hardlinks != 1:
            reasons.append("Real encoding requires exactly one known source hardlink.")
        if primary.pixel_format not in SUPPORTED_PIXEL_FORMATS:
            reasons.append(f"Unsupported or unknown source pixel format: {primary.pixel_format or 'unknown'}.")
        if item.duration_seconds is None or item.width is None or item.height is None:
            reasons.append("Source duration and dimensions are required for output validation.")
        if any(track["action"] != "copy" for track in audio_plan(item, job.preset, job.preserve_audio)):
            reasons.append("Real encoding cannot perform the preset's requested audio conversion safely.")
        if job.preset.rate_control == "crf":
            if job.preset.quality_value is None:
                reasons.append("CPU CRF preset has no quality value.")
        elif job.preset.rate_control == "abr":
            if job.preset.target_video_bitrate is None:
                reasons.append("ABR preset has no target bitrate.")
        else:
            reasons.append(f"Rate control {job.preset.rate_control} is unsupported by the CPU encoder.")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def primary_video(probe) -> StreamFacts | None:
        if probe is None:
            return None
        return next((s for s in probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)

    def validate_output(self, item: MediaItem, job: QueueJob, output_probe, output_path) -> list[str]:
        errors = []
        try:
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                errors.append("Output file is missing or empty.")
        except OSError:
            errors.append("Output file is missing or unreadable.")
        source_probe = item.probe
        source_video = self.primary_video(source_probe)
        output_video = self.primary_video(output_probe)
        if output_video is None:
            errors.append("Output has no primary video stream.")
        else:
            if output_video.codec.lower() not in {"hevc", "h265", "x265"}:
                errors.append(f"Output video codec is {output_video.codec}, expected HEVC.")
            if output_video.width != item.width or output_video.height != item.height:
                errors.append("Output resolution does not match the source.")
            if not confirmed_sdr(output_video):
                errors.append("Output does not validate as confirmed SDR.")
        if output_probe.duration_seconds is None or item.duration_seconds is None:
            errors.append("Source or output duration is unknown.")
        elif abs(output_probe.duration_seconds - item.duration_seconds) > max(2.0, item.duration_seconds * 0.001):
            errors.append("Output duration differs from the source by more than max(2 seconds, 0.1%).")
        if source_probe is not None:
            for kind, label in (("audio", "audio"), ("subtitle", "subtitle"),
                                ("attachment", "attachment"), ("data", "data"), ("video", "video")):
                if kind in {"subtitle", "attachment", "data"} and not job.preserve_subtitles:
                    continue
                expected = sum(s.kind == kind for s in source_probe.streams)
                actual = sum(s.kind == kind for s in output_probe.streams)
                if expected != actual:
                    errors.append(f"Output has {actual} {label} streams; expected {expected}.")
            if job.preserve_subtitles and len(output_probe.chapters) != len(source_probe.chapters):
                errors.append("Output chapter count does not match the source.")
        return errors
