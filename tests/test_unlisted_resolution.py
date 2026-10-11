"""Sources whose size is outside the standard resolution classes (e.g. 2560×1440)."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models.probe import with_inferred_scan_type
from app.models.queue import EnqueueRequest
from app.services.policy import PolicyEngine
from app.workers.real import RealEncoderWorker
from tests.test_real_encoding import (build_gpu_show_processor, build_real_app, gpu_preset,  # noqa: F401
                                      movie_item, movie_preset, probe_facts)

TABLE_REASON = "is not in this preset's bitrate table; choose a video bitrate."


def odd_size(probe, width=2560, height=1440):
    """The probe as a 2560×1440 source: no resolution class, scan type not signalled."""
    return probe.model_copy(update={"streams": [
        stream.model_copy(update={"width": width, "height": height, "resolution_class": None,
                                  "scan_type": "unknown"}) if stream.kind == "video" else stream
        for stream in probe.streams]})


def evaluate(item, preset, **changes):
    return PolicyEngine().evaluate(item=item, scope=preset.scope, preset=preset, effective_tags=[],
                                   preserve_audio=None, preserve_subtitles_and_metadata=True, **changes)


def odd_item(probe):
    probe = with_inferred_scan_type(odd_size(probe), "Film.2020.mkv")
    return movie_item(probe, width=2560, height=1440, resolution="Unknown")


def test_quality_preset_for_every_resolution_accepts_any_size(probe_facts):
    result = evaluate(odd_item(probe_facts), movie_preset())
    assert result.eligible, result.reasons
    assert not result.video_bitrate_required


def test_preset_limited_to_some_resolutions_still_excludes_other_sizes(probe_facts):
    preset = movie_preset().model_copy(update={"source_resolutions": ["1080p"]})
    result = evaluate(odd_item(probe_facts), preset)
    assert "Source size 2560×1440 is outside this preset's resolutions." in result.reasons


def test_bitrate_table_preset_asks_for_a_bitrate_and_plans_with_it(probe_facts):
    preset = gpu_preset()
    item = odd_item(probe_facts)
    missing = evaluate(item, preset)
    assert missing.video_bitrate_required and not missing.eligible
    assert any(TABLE_REASON in reason for reason in missing.reasons)

    chosen = evaluate(item, preset, video_bitrate=7_000_000)
    assert chosen.video_bitrate_required and chosen.eligible, chosen.reasons
    duration = item.duration_seconds
    assert duration is not None
    planned = chosen.planning_output_size
    assert planned is not None and planned > 7_000_000 * duration / 8  # video at the chosen rate, plus audio
    # A size inside the table never needs a bitrate, and a supplied one is ignored.
    listed = evaluate(movie_item(probe_facts), preset, video_bitrate=50_000_000)
    assert not listed.video_bitrate_required
    assert listed.planning_output_size == evaluate(movie_item(probe_facts), preset).planning_output_size


def test_unknown_dimensions_are_still_blocked(probe_facts):
    probe = probe_facts.model_copy(update={"streams": [
        stream.model_copy(update={"width": None, "height": None, "resolution_class": None})
        if stream.kind == "video" else stream for stream in probe_facts.streams]})
    result = evaluate(movie_item(probe, width=None, height=None, resolution="Unknown"), movie_preset())
    assert "Source resolution is unknown." in result.reasons


@pytest.mark.parametrize("value", [50_000, 250_000_000])
def test_chosen_bitrate_is_bounded(value):
    with pytest.raises(ValidationError):
        EnqueueRequest(media_ids=["x"], scope="show", preset_id="p", video_bitrate=value)


def test_gpu_job_encodes_an_odd_size_with_the_chosen_bitrate(tmp_path, monkeypatch, probe_facts):
    processor, spawned = build_gpu_show_processor(tmp_path, monkeypatch, odd_size(probe_facts))
    processor.scan_library()
    episode = processor.get_library().shows[0].seasons[0].episodes[0]
    assert (episode.width, episode.height, episode.resolution) == (2560, 1440, "Unknown")

    def request(video_bitrate=None):
        return EnqueueRequest(media_ids=[episode.id], scope="show", preset_id="show-streaming-quality",
                              video_bitrate=video_bitrate)

    refused = processor.queue_encode(request())
    assert not refused["added"] and TABLE_REASON in " ".join(refused["excluded"][0]["reasons"])
    preview = processor.evaluate_compression(episode.id, "show", "show-streaming-quality",
                                             video_bitrate=7_000_000)
    assert preview.eligible and preview.video_bitrate_required, preview.reasons

    added = processor.queue_encode(request(7_000_000))["added"][0]
    assert added["chosen_video_bitrate"] == 7_000_000
    processor.queue.revalidate()  # must keep the job queued with its chosen bitrate
    job = processor.queue.claim_next("gpu")
    assert job is not None and job.id == added["id"] and job.chosen_video_bitrate == 7_000_000
    worker = processor.queue.worker
    assert isinstance(worker, RealEncoderWorker)
    worker._execute(job)

    saved = next(item for item in processor.get_queue()["history"] if item["id"] == added["id"])
    assert saved["status"] == "completed", saved.get("error_message")
    command = spawned[0].command
    assert command[command.index("-b:v:0") + 1] == "7000000"


def test_cpu_job_for_a_listed_size_never_stores_a_chosen_bitrate(tmp_path, monkeypatch, probe_facts):
    application, _, _, _ = build_real_app(tmp_path, monkeypatch, probe_facts)
    with TestClient(application):
        processor = application.state.media_processor
        processor.scan_library()
        movie = processor.get_library().movies[0]
        added = processor.queue_encode(EnqueueRequest(media_ids=[movie.id], scope="movie",
            preset_id="movie-streaming-quality", video_bitrate=7_000_000))["added"][0]
        assert added["chosen_video_bitrate"] is None
