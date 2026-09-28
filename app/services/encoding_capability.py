"""Runtime execution capability checks layered after the PolicyEngine."""
from pathlib import Path

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


class _HEVCEncodeCapability:
    backend = ""
    allow_audio_conversion = False

    @staticmethod
    def primary_video(probe) -> StreamFacts | None:
        if probe is None:
            return None
        return next((s for s in probe.streams if s.kind == "video" and not s.dispositions.get("attached_pic")), None)

    def _backend_reasons(self, job: QueueJob) -> list[str]:
        return []

    def reasons(self, item: MediaItem, job: QueueJob) -> list[str]:
        reasons = []
        if not item.processing_supported:
            reasons.append("Real encoding prerequisites are unavailable.")
        reasons.extend(self._backend_reasons(job))
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
            return list(dict.fromkeys(reasons))
        primary = self.primary_video(probe)
        if primary is None:
            reasons.append("Source has no primary video stream.")
            return list(dict.fromkeys(reasons))
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
        if not self.allow_audio_conversion and any(
                track["action"] != "copy" for track in audio_plan(item, job.preset, job.preserve_audio)):
            reasons.append("Real encoding cannot perform the preset's requested audio conversion safely.")
        return list(dict.fromkeys(reasons))

    def validate_output(self, item: MediaItem, job: QueueJob, output_probe, output_path: Path) -> list[str]:
        errors = []
        try:
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                errors.append("Output file is missing or empty.")
        except OSError:
            errors.append("Output file is missing or unreadable.")

        source_probe = item.probe
        output_video = self.primary_video(output_probe)
        if output_video is None:
            errors.append("Output has no primary video stream.")
        else:
            if output_video.codec.lower() not in {"hevc", "h265", "x265"}:
                errors.append(f"Output video codec is {output_video.codec}, expected HEVC.")
            if output_video.width != item.width or output_video.height != item.height:
                errors.append("Output resolution does not match the source.")
            if output_video.scan_type != "progressive":
                errors.append(
                    f"Output scan type is {output_video.scan_type}; expected progressive."
                )
            if not confirmed_sdr(output_video):
                errors.append("Output does not validate as confirmed SDR.")
            actual_depth = output_video.hdr.bit_depth if output_video.hdr else None
            if actual_depth != job.preset.output_bit_depth:
                errors.append(
                    f"Output bit depth is {actual_depth or 'unknown'}; expected {job.preset.output_bit_depth} bit."
                )
            source_video = self.primary_video(source_probe)
            if source_video is not None:
                if source_video.color_range is not None and output_video.color_range != source_video.color_range:
                    errors.append(
                        f"Output colour range is {output_video.color_range or 'unknown'}; "
                        f"expected {source_video.color_range}."
                    )
                source_hdr = source_video.hdr
                output_hdr = output_video.hdr
                if source_hdr is not None:
                    for field, label in (
                        ("primaries", "colour primaries"),
                        ("transfer", "transfer characteristics"),
                        ("matrix", "matrix coefficients"),
                    ):
                        expected = getattr(source_hdr, field)
                        actual = getattr(output_hdr, field) if output_hdr is not None else None
                        if expected is not None and actual != expected:
                            errors.append(
                                f"Output {label} are {actual or 'unknown'}; expected {expected}."
                            )

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


class CPUEncodeCapability(_HEVCEncodeCapability):
    """Conservative CPU/libx265 HEVC slice."""

    backend = "cpu"

    def _backend_reasons(self, job: QueueJob) -> list[str]:
        reasons = []
        if job.backend != "cpu":
            reasons.append("Real CPU encoding supports the CPU backend only.")
            return reasons
        if job.preset.rate_control == "crf":
            if job.preset.quality_value is None:
                reasons.append("CPU CRF preset has no quality value.")
        elif job.preset.rate_control == "abr":
            if job.preset.target_video_bitrate is None:
                reasons.append("ABR preset has no target bitrate.")
        else:
            reasons.append(f"Rate control {job.preset.rate_control} is unsupported by the CPU encoder.")
        return reasons


class GPUEncodeCapability(_HEVCEncodeCapability):
    """Intel GPU (VA-API) HEVC slice for confirmed-SDR progressive sources."""

    backend = "qsv"
    allow_audio_conversion = True

    def _backend_reasons(self, job: QueueJob) -> list[str]:
        reasons = []
        if job.backend != "qsv":
            reasons.append("Real GPU encoding supports the GPU backend only.")
            return reasons
        if job.preset.rate_control == "icq":
            if job.preset.quality_value is None:
                reasons.append("GPU ICQ preset has no quality value.")
        elif job.preset.rate_control == "abr":
            if job.preset.target_video_bitrate is None:
                reasons.append("GPU ABR preset has no target bitrate.")
        else:
            reasons.append(f"Rate control {job.preset.rate_control} is unsupported by the GPU encoder.")
        return reasons

    def validate_output(self, item: MediaItem, job: QueueJob, output_probe, output_path: Path) -> list[str]:
        errors = super().validate_output(item, job, output_probe, output_path)
        video = self.primary_video(output_probe)
        if video is not None:
            pixel, profile = (("yuv420p10le", "Main 10") if job.preset.output_bit_depth == 10
                              else ("yuv420p", "Main"))
            if video.pixel_format != pixel or video.profile != profile:
                errors.append(f"GPU output must have pixel format {pixel} and profile {profile}.")
            encoder = next((value for key, value in video.metadata.items() if key.lower() == "encoder"), None)
            if encoder is not None and (not isinstance(encoder, str) or "hevc_vaapi" not in encoder.lower()):
                errors.append("GPU output encoder tag does not identify hevc_vaapi.")
        source_video = self.primary_video(item.probe)
        if item.probe is not None and source_video is not None:
            source_streams = [stream for stream in item.probe.streams
                              if job.preserve_subtitles or stream.kind in {"video", "audio"}]
            ordered = [source_video, *(s for s in source_streams if s.index != source_video.index)]
            actual = sorted(output_probe.streams, key=lambda stream: stream.index)
            if [s.kind for s in ordered] != [s.kind for s in actual]:
                errors.append("GPU output stream order does not match the mapping plan.")
            else:
                for source, output in zip(ordered, actual):
                    if source.index == source_video.index or source.kind == "audio":
                        continue
                    codec = "subrip" if source.codec == "mov_text" else source.codec
                    if output.codec != codec:
                        errors.append(f"GPU output {source.kind} codec/order differs from the mapped source.")
                if job.preserve_subtitles:
                    for source, output in zip(ordered, actual):
                        if source.language and source.language != output.language:
                            errors.append("GPU output stream language/order differs from the mapped source.")
                        if source.title and source.title != output.title:
                            errors.append("GPU output stream title/order differs from the mapped source.")
        expected = audio_plan(item, job.preset, job.preserve_audio)
        output_audio = sorted((stream for stream in output_probe.streams if stream.kind == "audio"),
                              key=lambda stream: stream.index)
        if len(expected) != len(output_audio):
            return errors
        for index, (plan, stream) in enumerate(zip(expected, output_audio)):
            wanted = str(plan["codec"]).lower()
            actual = stream.codec.lower()
            aliases = {"eac3": {"eac3", "e-ac-3"}, "aac": {"aac"}}
            if actual not in aliases.get(wanted, {wanted}):
                errors.append(f"Output audio stream {index} codec is {stream.codec}, expected {wanted}.")
            if plan["channels"] is not None and stream.channels != plan["channels"]:
                errors.append(
                    f"Output audio stream {index} has {stream.channels} channels; expected {plan['channels']}."
                )
        return errors
