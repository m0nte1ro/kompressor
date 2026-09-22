from __future__ import annotations

from app.models.media import (
    Episode,
    Movie,
)
from app.models.policy import EligibilityResult
from app.models.preset import CompressionPreset
from app.models.tags import QualityFloor


MediaItem = Movie | Episode


class PolicyEngine:
    def evaluate(
        self,
        *,
        item: MediaItem,
        scope: str,
        preset: CompressionPreset,
        effective_tags: list[str],
        preserve_audio: bool,
        preserve_subtitles: bool,
        quality_floor: QualityFloor | None = None,
    ) -> EligibilityResult:
        reasons: list[str] = []
        warnings: list[str] = []

        if "Quality CPU" in effective_tags and preset.backend != "cpu":
            reasons.append("Quality CPU policy requires a CPU preset.")

        if "Quality Floor" in effective_tags:
            if quality_floor is None:
                reasons.append("Quality Floor requires configured bitrate and resolution limits.")
            else:
                height = min(item.height, {"max_720p": 720, "max_1080p": 1080}.get(preset.resolution_policy, item.height))
                if height < quality_floor.minimum_height:
                    reasons.append("Output resolution is below the inherited Quality Floor.")
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

        if item.hardlinks > 1:
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
                "Interlaced content is blocked."
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

        if (
            item.video_bitrate
            < preset.minimum_source_bitrate
        ):
            reasons.append(
                "Source bitrate is below preset compression floor."
            )

        effective_preserve_audio = (
            preserve_audio
            or "Preserve Audio" in effective_tags
            or preset.audio_policy == "preserve"
        )

        if (
            "Preserve Audio" in effective_tags
            and not preserve_audio
        ):
            warnings.append(
                "Preserve Audio tag overrides the modal audio setting."
            )

        (
            estimated_output_size,
            estimated_saving,
            estimated_saving_percent,
        ) = self._estimate_output(
            item=item,
            preset=preset,
            preserve_audio=effective_preserve_audio,
        )

        if (
            estimated_saving_percent
            < preset.minimum_expected_saving_percent
        ):
            reasons.append(
                "Estimated saving is below preset minimum."
            )

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
            estimated_output_size=(
                estimated_output_size
            ),
            estimated_saving=estimated_saving,
            estimated_saving_percent=(
                estimated_saving_percent
            ),
            preserve_audio=(
                effective_preserve_audio
            ),
            preserve_subtitles=(
                preserve_subtitles
            ),
        )

    def _estimate_output(
        self,
        *,
        item: MediaItem,
        preset: CompressionPreset,
        preserve_audio: bool,
    ) -> tuple[int, int, float]:
        duration = item.duration_seconds

        source_video_bytes = int(
            (
                item.video_bitrate
                * duration
            )
            / 8
        )

        source_non_video_bytes = max(
            0,
            item.size - source_video_bytes,
        )

        target_video_bytes = int(
            (
                preset.target_video_bitrate
                * duration
            )
            / 8
        )

        if preserve_audio:
            target_non_video_bytes = (
                source_non_video_bytes
            )
        else:
            target_audio_bitrate = (
                preset.target_audio_bitrate
                or 384_000
            )

            target_non_video_bytes = int(
                (
                    target_audio_bitrate
                    * duration
                )
                / 8
            )

        estimated_output = int(
            (
                target_video_bytes
                + target_non_video_bytes
            )
            * 1.01
        )

        estimated_output = min(
            estimated_output,
            item.size,
        )

        estimated_saving = max(
            0,
            item.size - estimated_output,
        )

        if item.size:
            estimated_saving_percent = (
                estimated_saving
                / item.size
                * 100
            )
        else:
            estimated_saving_percent = 0.0

        return (
            estimated_output,
            estimated_saving,
            estimated_saving_percent,
        )
