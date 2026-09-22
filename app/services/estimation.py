"""Planning estimates, not measured encode sizes or perceptual quality scores."""
from app.models.preset import CompressionPreset


def audio_plan(item, preset: CompressionPreset, preserve_audio: bool) -> list[dict]:
    plan = []
    for index, track in enumerate(item.audio):
        bitrate = track.bitrate
        codec = track.codec.lower()
        target = preset.stereo_audio_bitrate if track.channels <= 2 else preset.target_audio_bitrate or 640000
        # Retain all languages/tracks and channel layouts; never silently downmix.
        copy = (preserve_audio or track.channels > 6 or bitrate is None or
                (codec in {"aac", "eac3", "ac3"} and bitrate <= target))
        plan.append({"track": index, "action": "copy" if copy else "encode",
                     "codec": codec if copy else "aac" if track.channels <= 2 else "eac3",
                     "channels": track.channels, "bitrate": bitrate if copy else target})
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
    return dict(estimated_output_size=output, estimated_saving=saving,
                estimated_saving_percent=saving / item.size * 100 if item.size else 0,
                estimated_output_size_low=output_low, estimated_output_size_high=output_high,
                estimated_saving_low=max(0, item.size-output_high),
                estimated_saving_high=max(0, item.size-output_low),
                estimate_basis="bitrate" if preset.rate_control == "abr" else "planning_range",
                audio_plan=plan)
