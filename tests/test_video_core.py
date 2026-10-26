from dataclasses import replace
from fractions import Fraction
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from ffmpeg_service import EngineCapabilities, EnginePaths, FFmpegError
from file_utils import OutputTransaction, allocate_output
from media_models import MediaInfo, MediaTask, StreamInfo, VideoOptions
from task_runner import TaskRunner, make_tasks
from video_converter import build_command, convert_video, output_dimensions, parse_progress, run_transcode, validate_output


@pytest.fixture
def capabilities():
    return EngineCapabilities(EnginePaths(Path("ffmpeg"), Path("ffprobe"), "test"), "test", "",
                              frozenset({"libx264", "aac", "libvpx-vp9", "libopus"}),
                              frozenset({"mp4", "matroska", "webm"}))


@pytest.fixture
def media(tmp_path):
    return MediaInfo(tmp_path / "source.mp4", ("mp4",), 10, 123,
                     (StreamInfo(2, "video", "h264", duration=10, width=1920, height=1080,
                                 average_frame_rate=Fraction(30000, 1001)),
                      StreamInfo(4, "audio", "aac", duration=10)))


@pytest.mark.parametrize("preset", ["mp4", "mkv", "webm"])
def test_commands_map_real_tracks_and_control_quality(media, capabilities, tmp_path, preset):
    output = tmp_path / ("file.part." + preset)
    args = build_command(media, output, VideoOptions(preset=preset), capabilities)
    assert args[args.index("-map") + 1] == "0:2"
    assert "0:4" in args and args[-1] == str(output)
    assert "-nostdin" in args and args[args.index("-fps_mode") + 1] == "passthrough"
    assert "-r" not in args
    assert args[args.index("-crf") + 1] == ("32" if preset == "webm" else "23")


def test_mute_does_not_require_an_audio_encoder(media, capabilities, tmp_path):
    capabilities = replace(capabilities, encoders=frozenset({"libx264"}))
    args = build_command(media, tmp_path / "out.mp4", VideoOptions(mute=True), capabilities)
    assert "-an" in args and "0:4" not in args
    with pytest.raises(FFmpegError, match="cannot execute"):
        build_command(media, tmp_path / "out.mp4", VideoOptions(), capabilities)


def test_size_preserves_display_aspect_and_does_not_enlarge(media):
    assert output_dimensions(media, VideoOptions(max_height=720)) == (1280, 720)
    assert output_dimensions(media, VideoOptions(max_height=2160)) == (1920, 1080)
    rotated = replace(media, streams=(replace(media.primary_video, rotation=90),))
    assert output_dimensions(rotated, VideoOptions(max_height=720)) == (404, 720)
    odd = replace(media, streams=(replace(media.primary_video, width=97, height=65),))
    assert output_dimensions(odd, VideoOptions()) == (96, 64)


@pytest.mark.parametrize("changes", [{"color_transfer": "smpte2084"}, {"pixel_format": "yuva420p"}])
def test_rejects_unsupported_color_before_encoding(media, capabilities, tmp_path, changes):
    media = replace(media, streams=(replace(media.primary_video, **changes),))
    with pytest.raises(FFmpegError) as exc:
        build_command(media, tmp_path / "out.mp4", VideoOptions(), capabilities)
    assert exc.value.code == "unsupported_color"


def test_progress_caps_at_99_until_commit_and_handles_unknown_duration():
    value = parse_progress({"out_time_us": "15000000", "speed": "2x", "progress": "end"}, 10)
    assert value.percent == 99 and value.remaining_seconds == 0
    assert parse_progress({"out_time": "00:00:02.5", "speed": "N/A"}, None).percent is None
    assert parse_progress({"out_time": "00:00:02.5"}, 10).processed_seconds == 2.5
    assert parse_progress({"out_time_us": "-1", "speed": "nan"}, 10).speed is None


def test_no_overwrite_publication_detects_late_writer(tmp_path):
    target = tmp_path / "output.mp4"
    with pytest.raises(FFmpegError):
        with OutputTransaction(target, (), False) as transaction:
            transaction.temporary.write_bytes(b"new result")
            target.write_bytes(b"other writer")
            transaction.commit()
    assert target.read_bytes() == b"other writer"
    assert not list(tmp_path.glob(".imadeo-*"))


def test_replacement_failure_preserves_old_output(tmp_path, monkeypatch):
    target = tmp_path / "output.mp4"
    target.write_bytes(b"old")

    def fail(*args):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(PermissionError):
        with OutputTransaction(target, (), True) as transaction:
            transaction.temporary.write_bytes(b"new")
            transaction.commit()
    assert target.read_bytes() == b"old" and not list(tmp_path.glob(".imadeo-*"))


def test_sources_and_hardlinks_cannot_be_overwritten(tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    alias = tmp_path / "alias.mp4"
    os.link(source, alias)
    for target in (source, alias):
        with pytest.raises(FFmpegError) as exc:
            with OutputTransaction(target, (source,), True):
                pytest.fail("Must reject the target")
        assert exc.value.code == "source_conflict"
    assert source.read_bytes() == b"source"


def test_batch_names_protect_all_inputs_and_reserve_duplicates(tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    tasks = make_tasks([source, source], tmp_path, VideoOptions(overwrite=True))
    assert [task.destination.name for task in tasks] == ["input_1.mp4", "input_2.mp4"]
    assert tasks[0].batch_id == tasks[1].batch_id and tasks[0].task_id != tasks[1].task_id


@pytest.mark.parametrize("cancel", [False, True])
def test_encoding_timeout_and_cancel_reap_owned_process(monkeypatch, cancel):
    processes = []
    real_popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set) if cancel else None
    if timer:
        timer.start()
    try:
        with pytest.raises(FFmpegError) as exc:
            run_transcode([sys.executable, "-c", "import time; time.sleep(30)"], duration=30,
                          cancel_event=stop, on_progress=lambda value: None, timeout=5 if cancel else 0.3)
        assert exc.value.code == ("cancelled" if cancel else "timeout")
        assert len(processes) == 1 and processes[0].poll() is not None
    finally:
        if timer:
            timer.cancel()
            timer.join()


def test_progress_reader_handles_large_stderr_and_failure_after_end():
    script = "import sys; sys.stderr.write('e'*200000); print('out_time_us=1000000\\nprogress=end'); sys.exit(1)"
    with pytest.raises(FFmpegError) as exc:
        run_transcode([sys.executable, "-c", script], duration=1,
                      cancel_event=threading.Event(), on_progress=lambda value: None)
    assert exc.value.code == "encode_failed" and len(exc.value.details) == 65536


def test_precancel_never_creates_output(capabilities, tmp_path):
    stop = threading.Event()
    stop.set()
    events = queue.Queue()
    task = MediaTask(tmp_path / "source.mp4", tmp_path / "out" / "output.mp4")
    result = convert_video(task, capabilities, cancel_event=stop, events=events)
    assert result.state == "cancelled" and not task.destination.parent.exists()
    terminals = [e for e in list(events.queue) if e.result]
    assert len(terminals) == 1 and terminals[0].result == result


def test_invalid_output_codec_is_rejected(media):
    bad = replace(media, streams=(replace(media.primary_video, codec_name="vp9", pixel_format="yuv420p"), media.primary_audio))
    with pytest.raises(FFmpegError) as exc:
        validate_output(media, bad, VideoOptions())
    assert exc.value.code == "invalid_output"


def test_runner_rejects_overlapping_batches(capabilities, tmp_path, monkeypatch):
    import task_runner
    entered, release = threading.Event(), threading.Event()
    errors = []

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release worker")
        return None

    monkeypatch.setattr(task_runner, "convert_video", blocked)
    runner = TaskRunner(capabilities)
    tasks = make_tasks([tmp_path / "source.mp4"], tmp_path / "out", VideoOptions())

    def work():
        try:
            runner.run(tasks)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(3)
        with pytest.raises(FFmpegError) as exc:
            runner.run(tasks)
        assert exc.value.code == "batch_running"
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
