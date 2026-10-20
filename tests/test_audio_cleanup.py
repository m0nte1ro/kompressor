"""Portuguese-only lossless remux: selection, refusal and real ffmpeg checks."""
from pathlib import Path
import shutil
import subprocess

import pytest

from app.tools.audio_cleanup import CleanupError, cleanup, main, select_audio, probe, language


def test_select_one_portuguese_stream_even_when_english_is_default():
    info = {"streams": [
        {"index": 0, "codec_type": "video"},
        {"index": 1, "codec_type": "audio", "tags": {"language": "eng"},
         "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "tags": {"language": "por"},
         "disposition": {"default": 0}},
    ]}
    assert select_audio(info, None)["index"] == 2
    assert select_audio(info, 1)["index"] == 1


def test_untagged_and_duplicate_languages_require_explicit_choice():
    with pytest.raises(CleanupError, match="exactly one"):
        select_audio({"streams": [{"index": 3, "codec_type": "audio"}]}, None)
    tracks = [{"index": i, "codec_type": "audio", "tags": {"language": "por"}}
              for i in (4, 5)]
    with pytest.raises(CleanupError, match="exactly one"):
        select_audio({"streams": tracks}, None)
    with pytest.raises(CleanupError, match="not an audio"):
        select_audio({"streams": tracks}, 99)


@pytest.fixture
def two_audio_mkv(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools unavailable")
    target = tmp_path / "PhineasAndFerb.S01.E01.1080P.mkv"
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
        "color=c=blue:s=64x64:r=1:d=1", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1", "-f", "lavfi", "-i",
        "sine=frequency=660:duration=1", "-map", "0:v:0", "-map", "1:a:0",
        "-map", "2:a:0", "-metadata:s:a:0", "language=eng",
        "-metadata:s:a:1", "language=por", "-disposition:a:0", "default",
        "-disposition:a:1", "0", "-c:v", "ffv1", "-c:a", "pcm_s16le",
        str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    return target


def test_remux_keeps_portuguese_only_default_and_copies_video(two_audio_mkv):
    original = two_audio_mkv.read_bytes()
    result = cleanup(two_audio_mkv)
    assert "source unchanged" in result
    assert two_audio_mkv.read_bytes() == original
    generated = list(two_audio_mkv.parent.glob("*.audio-cleanup.mkv"))
    assert len(generated) == 1
    streams = probe(generated[0])["streams"]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    assert len(audios) == 1
    assert language(audios[0]) == "por"
    assert audios[0]["disposition"]["default"] == 1


def test_explicit_replace_retains_original_untouched_backup(two_audio_mkv):
    original = two_audio_mkv.read_bytes()
    result = cleanup(two_audio_mkv, replace=True)
    assert "Original backup RETAINED" in result
    backups = list(two_audio_mkv.parent.glob("*.original.mkv"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    audios = [s for s in probe(two_audio_mkv)["streams"] if s["codec_type"] == "audio"]
    assert len(audios) == 1
    assert language(audios[0]) == "por"


def test_preview_does_not_mutate_files(two_audio_mkv, capsys):
    before = two_audio_mkv.read_bytes()
    assert main([str(two_audio_mkv), "--plan"]) == 0
    assert "keep ONLY #2 (por)" in capsys.readouterr().out
    assert two_audio_mkv.read_bytes() == before
    assert len(list(two_audio_mkv.parent.iterdir())) == 1


def test_directory_selection_avoids_generated_outputs(tmp_path):
    from app.tools.audio_cleanup import candidates
    root = tmp_path / "shows"
    root.mkdir()
    (root / "episode.mkv").touch()
    (root / "episode.abc.original.mkv").touch()
    (root / "episode.abc.audio-cleanup.mkv").touch()
    assert [p.name for p in candidates(root, True)] == ["episode.mkv"]
