from __future__ import annotations

from app.models.media import (
    Episode,
    Movie,
)
from app.models.media import spatial_resolution
from app.models.policy import EligibilityResult
from app.models.preset import CompressionPreset
from app.models.tags import QualityFloor
from app.services.estimation import estimate


MediaItem = Movie | Episode


class PolicyEngine:
    def evaluate(
        self,
        *,
        item: MediaItem,
        scope: str,
        preset: CompressionPreset,
        effective_tags: list[str],
        preserve_audio: bool | None,
        preserve_subtitles: bool,
        quality_floor: QualityFloor | None = None,
    ) -> EligibilityResult:
        reasons: list[str] = []
        warnings: list[str] = []
        if not item.processing_supported:
            reasons.append("Read-only filesystem inventory: encoding is not enabled.")
        if item.hdr in {"hdr10plus", "hlg", "unknown"}:
            reasons.append("HDR signalling is unsupported or uncertain; transcoding is blocked.")
        if item.probe:
            for stream in item.probe.streams:
                if stream.kind == "video" and stream.hdr and stream.hdr.classify().base != "sdr" and stream.hdr.classify().uncertain:
                    reasons.append("Dynamic HDR metadata is uncertain; transcoding is blocked.")
                    break
        if item.duration_seconds is None:
            reasons.append("Source duration is unknown.")
        if item.interlaced is None:
            reasons.append("Source scan type is unknown.")
        if spatial_resolution(item) not in preset.source_resolutions:
            reasons.append("Source resolution is not supported by this preset. Configure explicit applicability.")
        if item.hdr and preset.hdr_support == "sdr_only":
            reasons.append("This preset supports SDR sources only.")
        elif item.hdr == "hdr10":
            warnings.append("HDR10 pipeline is experimental and must be validated on actual output.")
        if preset.hdr_policy == "tone_map_to_sdr":
            reasons.append("HDR to SDR tone mapping is not implemented in this workflow.")
        if preset.rate_control != "abr":
            warnings.append("Planning range only: quality-based output may fall outside it. Actual savings must be checked after encoding.")
        if preset.backend == "qsv":
            warnings.append("QSV quality and rate-control support require validation on your Intel hardware.")

        if "Quality CPU" in effective_tags and preset.backend != "cpu":
            reasons.append("Quality CPU policy requires a CPU preset.")

        if "Quality Floor" in effective_tags:
            if quality_floor is None:
                reasons.append("Quality Floor requires configured bitrate and resolution limits.")
            elif item.height is None:
                reasons.append("Source resolution is unknown; Quality Floor cannot be checked.")
            else:
                height = min(item.height, {
                    "max_480p": 480,
                    "max_576p": 576,
                    "max_720p": 720,
                    "max_1080p": 1080,
                    "max_2160p": 2160,
                }.get(preset.target_resolution, item.height))
                if height < quality_floor.minimum_height:
                    reasons.append("Output resolution is below the inherited Quality Floor.")
                if preset.rate_control != "abr":
                    reasons.append("Quality Floor bitrate cannot be guaranteed by CRF/ICQ. Use a bitrate preset or revise the tag.")
                else:
                    # Preset validation requires a target bitrate for ABR.
                    assert preset.target_video_bitrate is not None
                    if preset.target_video_bitrate < quality_floor.minimum_video_bitrate:
                        reasons.append("Target video bitrate is below the inherited Quality Floor.")

        if preset.scope != scope:
            reasons.append(
                "Preset scope does not match media type."
            )

        if not preset.enabled:
            reasons.append(
                "Preset is disabled."
            )

        if item.hardlinks is None:
            reasons.append("Source hardlink count is unknown.")
        elif item.hardlinks > 1:
            reasons.append(
                "File is hardlinked / may still be seeding."
            )

        if "Preserve A/V" in effective_tags:
            reasons.append(
                "Preserve A/V policy blocks all transcoding."
            )

        elif "Preserve Video" in effective_tags:
            reasons.append(
                "Preserve Video policy blocks video transcoding."
            )

        if (
            scope == "movie"
            and item.height is not None
            and item.height >= 2160
            and isinstance(item, Movie)
            and item.source
            and "REMUX" in item.source.upper()
        ):
            reasons.append(
                "2160p REMUX movies are automatically protected."
            )

        if item.hdr in {
            "dolby_vision",
            "dolby_vision_hdr10",
        }:
            reasons.append(
                "Dolby Vision transcoding is blocked."
            )

        if item.interlaced:
            reasons.append(
                "Interlaced source requires deinterlacing; no validated pipeline is enabled"
            )

        if (
            item.video_codec.lower() in {
                "hevc",
                "h265",
                "x265",
            }
            and not preset.allow_hevc_reencode
        ):
            reasons.append(
                "Preset does not allow HEVC recompression."
            )

        if item.video_bitrate is None:
            reasons.append("Source video bitrate is unknown.")
        elif (
            item.video_bitrate
            < preset.minimum_source_bitrate
        ):
            reasons.append(
                "Source bitrate is below preset compression floor."
            )

        effective_preserve_audio = (
            preserve_audio is True
            or (preserve_audio is not False and preset.preserve_audio_by_default)
            or "Preserve Audio" in effective_tags
            or (preset.audio_policy == "preserve" and preset.audio_conversion_policy == "preserve")
        )

        if (
            "Preserve Audio" in effective_tags
            and preserve_audio is not True
        ):
            warnings.append(
                "Preserve Audio tag overrides the modal audio setting."
            )

        estimates = estimate(item, preset, effective_preserve_audio)
        if "Preserve A/V" in effective_tags:
            estimates.update(
                estimated_output_size=None,
                estimated_saving=None,
                estimated_saving_percent=None,
                estimated_output_size_low=None,
                estimated_output_size_high=None,
                estimated_saving_low=None,
                estimated_saving_high=None,
                estimate_basis="unknown",
                planning_output_size=None,
                planning_saving=None,
                planning_saving_percent=None,
            )
        saving_percent = estimates["estimated_saving_percent"]
        if saving_percent is None:
            saving_percent = estimates["planning_saving_percent"]
        if saving_percent is not None and saving_percent < preset.minimum_expected_saving_percent:
            if preset.rate_control == "abr":
                reasons.append("Estimated saving is below preset minimum.")
            else:
                warnings.append("Planning estimate is below the minimum saving. Actual output must meet the threshold before replacement.")

        return EligibilityResult(
            eligible=not reasons,
            reasons=reasons,
            warnings=warnings,
            preset_id=preset.id,
            preset_name=preset.name,
            backend=preset.backend,
            destination_codec=(
                preset.destination_codec
            ),
            source_size=item.size,
            **estimates,
            preserve_audio=(
                effective_preserve_audio
            ),
            preserve_subtitles=(
                preserve_subtitles
            ),
        )
