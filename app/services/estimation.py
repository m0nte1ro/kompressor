"""Planning estimates, not measured encode sizes or perceptual quality scores."""
from app.models.preset import CompressionPreset


def audio_plan(item, preset: CompressionPreset, preserve_audio: bool) -> list[dict]:
    rules = preset.efficient_audio_rules
    plan = []
    for index, track in enumerate(item.audio):
        bitrate = track.bitrate
        codec = track.codec.lower()
        target = preset.stereo_audio_bitrate if track.channels <= 2 else preset.target_audio_bitrate or 640000
        copy = preserve_audio or (
            rules.channel_handling == "preserve"
            and (
                bitrate is None and rules.copy_unknown_bitrate
                or rules.copy_channels_above is not None and track.channels > rules.copy_channels_above
                or rules.copy_if_bitrate_at_or_below_target and codec in rules.copy_codecs
                and bitrate is not None and bitrate <= target
            )
        )
        channels = track.channels if rules.channel_handling == "preserve" else min(track.channels, 2)
        plan.append({"track": index, "action": "copy" if copy else "encode",
                     "codec": codec if copy else rules.mono_stereo_codec if track.channels <= 2 else rules.multichannel_codec,
                     "channels": channels, "bitrate": bitrate if copy else target})
    return plan


def estimate(item, preset: CompressionPreset, preserve_audio: bool) -> dict:
    plan = audio_plan(item, preset, preserve_audio)
    duration = item.duration_seconds
    # Container/subtitle/unknown-stream residual is retained in either audio mode.
    known_audio = sum(t.bitrate or 0 for t in item.audio) * duration / 8
    source_video = item.video_bitrate * duration / 8
    residual = max(0, item.size - source_video - known_audio)
    audio_bytes = sum(t["bitrate"] or 0 for t in plan) * duration / 8
    low = preset.target_video_bitrate if preset.rate_control == "abr" else preset.planning_video_bitrate_low
    high = preset.target_video_bitrate if preset.rate_control == "abr" else preset.planning_video_bitrate_high
    output_low = int((low * duration / 8 + audio_bytes + residual) * 1.01)
    output_high = int((high * duration / 8 + audio_bytes + residual) * 1.01)
    output = (output_low + output_high) // 2
    saving = max(0, item.size - output)
    planning_percent = saving / item.size * 100 if item.size else 0
    quality_mode = preset.rate_control != "abr"
    return dict(estimated_output_size=None if quality_mode else output,
                estimated_saving=None if quality_mode else saving,
                estimated_saving_percent=None if quality_mode else planning_percent,
                estimated_output_size_low=output_low, estimated_output_size_high=output_high,
                estimated_saving_low=max(0, item.size-output_high),
                estimated_saving_high=max(0, item.size-output_low),
                estimate_basis="planning_range" if quality_mode else "bitrate",
                planning_output_size=output if quality_mode else None,
                planning_saving=saving if quality_mode else None,
                planning_saving_percent=planning_percent if quality_mode else None,
                audio_plan=plan)
