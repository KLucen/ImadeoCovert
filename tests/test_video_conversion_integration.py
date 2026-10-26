from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from ffmpeg_service import FFmpegError, inspect_engine, probe_media
from media_models import MediaTask, VideoOptions
from task_runner import TaskRunner, make_tasks
from video_converter import convert_video
from test_video_integration import engine, ffmpeg

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def capabilities(engine):
    return inspect_engine(engine)


def sample(engine, path, *, audio=True, size="128x72", duration="0.6"):
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac"]
    ffmpeg(engine, *args, "-c:v", "libx264", "-preset", "ultrafast", str(path))
    return path


@pytest.mark.parametrize("preset", ["mp4", "mkv", "webm"])
@pytest.mark.parametrize("audio,mute", [(False, False), (True, False), (True, True)])
def test_real_conversion_and_decoding(capabilities, tmp_path, preset, audio, mute):
    source = sample(capabilities.paths, tmp_path / "中文 source (1).mp4", audio=audio)
    original = hashlib.sha256(source.read_bytes()).digest()
    task = MediaTask(source, tmp_path / "results" / f"结果.{preset}",
                     VideoOptions(preset=preset, mute=mute, speed="fast"))
    events = queue.Queue()
    result = convert_video(task, capabilities, events=events)
    assert result.state == "succeeded", (result.error_code, result.error_message, result.details)
    assert result.media.path == task.destination
    assert result.media.primary_video.average_frame_rate == 15
    assert bool(result.media.audio_streams) == (audio and not mute)
    ffmpeg(capabilities.paths, "-xerror", "-i", str(task.destination), "-f", "null", "-")
    assert hashlib.sha256(source.read_bytes()).digest() == original
    recorded = list(events.queue)
    assert [event.state for event in recorded if event.result] == ["succeeded"]
    assert all(event.task_id == task.task_id and event.batch_id == task.batch_id for event in recorded)
    assert recorded[-1].progress.percent == 100
    assert all(event.progress.percent < 100 for event in recorded[:-1] if event.progress and event.progress.percent is not None)
    assert not list(task.destination.parent.glob(".imadeo-*"))


def test_rotation_is_applied_once_and_scaling_respects_portrait(capabilities, tmp_path):
    source = sample(capabilities.paths, tmp_path / "source.mp4")
    rotated = tmp_path / "rotated.mp4"
    ffmpeg(capabilities.paths, "-display_rotation:v:0", "90", "-i", str(source), "-map", "0", "-c", "copy", str(rotated))
    task = MediaTask(rotated, tmp_path / "output.mp4", VideoOptions(max_height=96))
    result = convert_video(task, capabilities)
    assert result.state == "succeeded", result
    assert result.media.primary_video.display_size == (54, 96)
    assert result.media.primary_video.rotation == 0


def test_non_square_pixels_are_normalized_without_stretch(capabilities, tmp_path):
    source = tmp_path / "anamorphic.mkv"
    ffmpeg(capabilities.paths, "-f", "lavfi", "-i", "testsrc2=size=120x80:rate=12:duration=0.5",
           "-vf", "setsar=4/3", "-c:v", "libx264", str(source))
    result = convert_video(MediaTask(source, tmp_path / "output.mp4"), capabilities)
    assert result.state == "succeeded", result
    assert result.media.primary_video.display_size == (160, 80)
    assert result.media.primary_video.sample_aspect_ratio == 1


def test_odd_dimensions_are_adjusted_to_even(capabilities, tmp_path):
    source = tmp_path / "odd.mkv"
    ffmpeg(capabilities.paths, "-f", "lavfi", "-i", "testsrc=size=97x65:rate=12:duration=0.5",
           "-c:v", "ffv1", str(source))
    result = convert_video(MediaTask(source, tmp_path / "output.mp4"), capabilities)
    assert result.state == "succeeded", result
    assert result.media.primary_video.display_size == (96, 64)


def test_batch_continues_after_failure_and_can_run_again(capabilities, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"invalid media")
    good = sample(capabilities.paths, tmp_path / "good.mp4")
    runner = TaskRunner(capabilities)
    first = runner.run(make_tasks([bad, good], tmp_path / "out", VideoOptions()))
    assert [result.state for result in first] == ["failed", "succeeded"]
    second = runner.run(make_tasks([good], tmp_path / "out", VideoOptions()))
    assert second[0].state == "succeeded" and second[0].destination.name == "good_1.mp4"
    assert first[0].batch_id != second[0].batch_id


def test_cancel_active_conversion_preserves_old_result_and_cancels_queue(capabilities, tmp_path, monkeypatch):
    import video_converter
    source = sample(capabilities.paths, tmp_path / "long.mp4", size="320x180", duration="20")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    old = output_dir / "long.webm"
    old.write_bytes(b"previous good result")
    tasks = make_tasks([source, source], output_dir, VideoOptions(preset="webm", speed="slow", overwrite=True))
    stop = threading.Event()
    entered = threading.Event()
    original_run = video_converter.run_transcode
    processes = []
    original_popen = video_converter.subprocess.Popen

    def record(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def run(*args, **kwargs):
        def progress(value):
            entered.set()
            kwargs["on_progress"](value)
        forwarded = dict(kwargs)
        forwarded["on_progress"] = progress
        return original_run(*args, **forwarded)

    monkeypatch.setattr(video_converter, "run_transcode", run)
    monkeypatch.setattr(video_converter.subprocess, "Popen", record)
    results = []
    runner = TaskRunner(capabilities)
    worker = threading.Thread(target=lambda: results.extend(runner.run(tasks, cancel_event=stop)))
    worker.start()
    try:
        assert entered.wait(15), "Expected real FFmpeg progress"
        stop.set()
        worker.join(3)
        assert not worker.is_alive(), "Cancellation exceeded the 3-second target"
    finally:
        stop.set()
        worker.join(10)
    assert [result.state for result in results] == ["cancelled", "cancelled"]
    assert all(process.poll() is not None for process in processes)
    assert old.read_bytes() == b"previous good result"
    assert not tasks[1].destination.exists()
    assert not list(output_dir.glob(".imadeo-*"))
    restarted = runner.run(make_tasks([source], output_dir, VideoOptions(speed="fast", max_height=32, mute=True)))
    assert restarted[0].state == "succeeded" and restarted[0].batch_id != tasks[0].batch_id


@pytest.mark.parametrize("failure", ["encode", "validate", "commit", "disk"])
def test_failure_at_each_phase_preserves_old_output(capabilities, tmp_path, monkeypatch, failure):
    import video_converter
    source = sample(capabilities.paths, tmp_path / "source.mp4")
    target = tmp_path / "output.mp4"
    target.write_bytes(b"old result")

    def fail(*args, **kwargs):
        raise FFmpegError("injected_failure", "Simulated failure")

    if failure == "encode":
        monkeypatch.setattr(video_converter, "run_transcode", fail)
    elif failure == "validate":
        monkeypatch.setattr(video_converter, "validate_output", fail)
    elif failure == "commit":
        monkeypatch.setattr(video_converter.OutputTransaction, "commit", fail)
    else:
        def disk_full(*args):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(os, "fsync", disk_full)
    result = convert_video(MediaTask(source, target, VideoOptions(overwrite=True)), capabilities)
    assert result.state == "failed" and target.read_bytes() == b"old result"
    if failure == "disk":
        assert result.error_code == "file_error"
    assert not list(tmp_path.glob(".imadeo-*"))


def test_batch_cannot_replace_a_later_source(capabilities, tmp_path):
    first = sample(capabilities.paths, tmp_path / "first.mp4")
    second = sample(capabilities.paths, tmp_path / "second.mp4")
    before = second.read_bytes()
    task = MediaTask(first, second, VideoOptions(overwrite=True))
    next_task = MediaTask(second, tmp_path / "out.mp4", batch_id=task.batch_id)
    results = TaskRunner(capabilities).run((task, next_task))
    assert results[0].error_code == "source_conflict" and results[1].state == "succeeded"
    assert second.read_bytes() == before


def test_variable_frame_timestamps_are_preserved(capabilities, tmp_path):
    source = tmp_path / "vfr.mp4"
    ffmpeg(capabilities.paths, "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=10:duration=1",
           "-vf", "setpts='if(lt(N,5),N/(10*TB),(0.5+(N-5)/5)/TB)'",
           "-fps_mode", "vfr", "-c:v", "libx264", str(source))
    task = MediaTask(source, tmp_path / "output.mp4")
    result = convert_video(task, capabilities)
    assert result.state == "succeeded", result

    def timestamps(path):
        raw = subprocess.check_output([str(capabilities.paths.ffprobe), "-v", "error", "-select_streams", "v:0",
                                       "-show_entries", "packet=pts_time", "-of", "json", str(path)], timeout=10,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        points = sorted(float(packet["pts_time"]) for packet in json.loads(raw)["packets"])
        return [point - points[0] for point in points]

    original, converted = timestamps(source), timestamps(task.destination)
    assert len(set(round(b - a, 3) for a, b in zip(original, original[1:]))) > 1
    assert converted == pytest.approx(original, abs=0.01)


def test_cli_converts_from_another_working_directory(capabilities, tmp_path):
    source = sample(capabilities.paths, tmp_path / "source.mp4")
    script = Path(__file__).resolve().parents[1] / "scripts" / "convert_video.py"
    result = subprocess.run([sys.executable, str(script), str(source), "--output-dir", str(tmp_path / "output"),
                             "--format", "webm", "--speed", "fast", "--mute"],
                            cwd=tmp_path, capture_output=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["state"] == "succeeded" and events[-1]["progress"]["percent"] == 100
    assert (tmp_path / "output" / "source.webm").is_file()


def test_successful_overwrite_replaces_only_after_validation(capabilities, tmp_path):
    source = sample(capabilities.paths, tmp_path / "source.mp4")
    output = tmp_path / "output.mp4"
    output.write_bytes(b"old result")
    result = convert_video(MediaTask(source, output, VideoOptions(overwrite=True)), capabilities)
    assert result.state == "succeeded", result
    assert probe_media(output, capabilities.paths).primary_video.codec_name == "h264"


def test_unexpected_file_failure_does_not_abort_the_batch(capabilities, tmp_path, monkeypatch):
    import video_converter
    sources = [sample(capabilities.paths, tmp_path / name) for name in ("first.mp4", "second.mp4")]
    original = video_converter.build_command

    def raise_once(media, *args, **kwargs):
        if media.path.name == "first.mp4":
            raise RuntimeError("simulated unexpected error")
        return original(media, *args, **kwargs)

    monkeypatch.setattr(video_converter, "build_command", raise_once)
    results = TaskRunner(capabilities).run(make_tasks(sources, tmp_path / "out", VideoOptions()))
    assert results[0].error_code == "internal_error" and results[1].state == "succeeded"
    assert not list((tmp_path / "out").glob(".imadeo-*"))
