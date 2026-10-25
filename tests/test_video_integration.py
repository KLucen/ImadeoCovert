"""Tiny synthetic media; no downloaded or personal media is used."""

import os
from pathlib import Path
import subprocess

import pytest

from ffmpeg_service import FFmpegError, discover_engine, inspect_engine, probe_media
from media_models import VIDEO_PRESETS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine():
    try:
        capabilities = inspect_engine(discover_engine())
    except FFmpegError as exc:
        if os.environ.get("IMADEO_REQUIRE_FFMPEG") == "1":
            pytest.fail(f"Required FFmpeg is unavailable: {exc}")
        pytest.skip(f"Install the pinned FFmpeg build to run integrations: {exc}")
    missing = {p.id: capabilities.missing_for(p) for p in VIDEO_PRESETS if capabilities.missing_for(p)}
    if missing:
        if os.environ.get("IMADEO_REQUIRE_FFMPEG") == "1":
            pytest.fail(f"Required encoders/muxers are missing: {missing}")
        pytest.skip(f"Install the pinned build: {missing}")
    return capabilities.paths


def ffmpeg(engine, *args):
    result = subprocess.run([str(engine.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", *args],
                            capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("container", ["mp4", "mkv", "webm"])
@pytest.mark.parametrize("with_audio", [False, True])
def test_probe_real_container_and_tracks(engine, tmp_path, container, with_audio):
    path = tmp_path / f"中文 sample (1).{container}"
    preset = next(p for p in VIDEO_PRESETS if p.id == container)
    args = ["-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12:duration=0.5"]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:a", preset.audio_encoder]
    args += ["-c:v", preset.video_encoder]
    if container == "webm":
        args += ["-deadline", "realtime", "-cpu-used", "8"]
    args += ["-t", "0.5", str(path)]
    ffmpeg(engine, *args)
    info = probe_media(path, engine)
    assert info.primary_video.codec_name == ("vp9" if container == "webm" else "h264")
    assert info.primary_video.display_size == (96, 64)
    assert info.primary_video.average_frame_rate == 12
    assert 0.4 < info.duration < 0.8
    assert bool(info.audio_streams) == with_audio
    assert info.size_bytes == path.stat().st_size
    if with_audio:
        assert info.primary_audio.codec_name == ("opus" if container == "webm" else "aac")


def test_rotation_metadata_and_multiple_audio_tracks(engine, tmp_path):
    source = tmp_path / "source.mp4"
    rotated = tmp_path / "rotated.mp4"
    ffmpeg(engine, "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=12:duration=0.5",
           "-f", "lavfi", "-i", "sine=duration=0.5", "-map", "0:v", "-map", "1:a", "-map", "1:a",
           "-c:v", "libx264", "-c:a", "aac", "-disposition:a:0", "0", "-disposition:a:1", "default", str(source))
    ffmpeg(engine, "-display_rotation:v:0", "90", "-i", str(source),
           "-map", "0", "-c", "copy", str(rotated))
    info = probe_media(rotated, engine)
    assert info.primary_video.rotation in (90, 270)
    assert info.primary_video.display_size == (64, 96)
    assert len(info.audio_streams) == 2 and info.primary_audio.index == 2


def test_damaged_file_is_not_reported_as_video(engine, tmp_path):
    path = tmp_path / "damaged.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(FFmpegError) as exc:
        probe_media(path, engine)
    assert exc.value.code == "process_failed" and exc.value.details


def test_audio_only_file_is_rejected(engine, tmp_path):
    path = tmp_path / "audio.m4a"
    ffmpeg(engine, "-f", "lavfi", "-i", "sine=duration=0.2", "-c:a", "aac", str(path))
    with pytest.raises(FFmpegError) as exc:
        probe_media(path, engine)
    assert exc.value.code == "no_video"
