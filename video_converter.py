"""Synchronous, cancellable video conversion core; call from a worker thread.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from dataclasses import replace
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Callable

from ffmpeg_service import EngineCapabilities, FFmpegError, probe_media
from file_utils import OutputTransaction
from media_models import (ConversionProgress, ConversionResult, MediaInfo, MediaTask,
                          TaskEvent, VideoOptions, VideoPreset, VIDEO_PRESETS)


def validate_options(options: VideoOptions) -> VideoPreset:
    preset = next((p for p in VIDEO_PRESETS if p.id == options.preset), None)
    if preset is None or options.quality not in {"high", "balanced", "compact"} or options.speed not in {"fast", "balanced", "slow"}:
        raise FFmpegError("invalid_options", "Unknown output, quality or speed preset.")
    if options.max_height is not None and (type(options.max_height) is not int or options.max_height < 2):
        raise FFmpegError("invalid_options", "Maximum height must be an integer of at least two pixels.")
    if options.timeout is not None and (not math.isfinite(options.timeout) or options.timeout <= 0):
        raise FFmpegError("invalid_options", "Timeout must be finite and positive.")
    return preset


def output_dimensions(media: MediaInfo, options: VideoOptions) -> tuple[int, int]:
    video = media.primary_video
    if video is None or video.display_size is None:
        raise FFmpegError("no_video", "A valid video stream is required.")
    if not math.isclose(video.rotation % 90, 0, abs_tol=0.001):
        raise FFmpegError("unsupported_rotation", "Only quarter-turn rotation is supported in this version.")
    width, height = video.display_size
    if min(width, height) < 2:
        raise FFmpegError("unsupported_dimensions", "The video is too small for the selected encoder.")
    factor = min(1.0, options.max_height / height) if options.max_height else 1.0
    return max(2, int(width * factor) // 2 * 2), max(2, int(height * factor) // 2 * 2)


def build_command(media: MediaInfo, temporary: Path, options: VideoOptions,
                  engine: EngineCapabilities) -> list[str]:
    preset = validate_options(options)
    video, audio = media.primary_video, media.primary_audio
    if video is None:
        raise FFmpegError("no_video", "A valid video stream is required.")
    if video.is_hdr or video.has_alpha:
        raise FFmpegError("unsupported_color", "HDR, Dolby Vision and transparent video require a future conversion mode.")
    required = {preset.video_encoder}
    if audio and not options.mute:
        required.add(preset.audio_encoder)
    missing = required - engine.encoders
    if missing or preset.muxer not in engine.muxers:
        raise FFmpegError("unsupported_preset", "The engine cannot execute this output preset.", ", ".join(sorted(missing)))
    width, height = output_dimensions(media, options)
    command = [str(engine.paths.ffmpeg), "-hide_banner", "-loglevel", "warning", "-xerror", "-nostdin",
               "-nostats", "-stats_period", "0.2", "-progress", "pipe:1", "-y",
               "-i", str(media.path), "-map", f"0:{video.index}"]
    if audio and not options.mute:
        command += ["-map", f"0:{audio.index}", "-c:a", preset.audio_encoder, "-b:a", "192k"]
    else:
        command += ["-an"]
    command += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                "-c:v", preset.video_encoder, "-pix_fmt", "yuv420p",
                "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1", "-fps_mode", "passthrough"]
    if preset.video_encoder == "libx264":
        command += ["-crf", str({"high": 18, "balanced": 23, "compact": 28}[options.quality]),
                    "-preset", {"fast": "veryfast", "balanced": "medium", "slow": "slow"}[options.speed]]
    else:
        command += ["-crf", str({"high": 24, "balanced": 32, "compact": 40}[options.quality]),
                    "-b:v", "0", "-deadline", "good", "-cpu-used",
                    str({"fast": 5, "balanced": 2, "slow": 0}[options.speed]), "-row-mt", "1"]
    if preset.id == "mp4":
        command += ["-movflags", "+faststart"]
    return command + ["-f", preset.muxer, str(temporary)]


def parse_progress(values: dict[str, str], duration: float | None) -> ConversionProgress:
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) and result >= 0 else None
        except (ValueError, TypeError):
            return None

    microseconds = number(values.get("out_time_us"))
    seconds = (microseconds or 0) / 1_000_000
    if microseconds is None:
        try:
            hours, minutes, trailing = values.get("out_time", "").split(":")
            seconds = number(float(hours) * 3600 + float(minutes) * 60 + float(trailing)) or 0
        except ValueError:
            pass
    speed = number(values.get("speed", "").rstrip("x")) or None
    percent = min(99.0, seconds / duration * 100) if duration and duration > 0 else None
    remaining = max(0.0, duration - seconds) / speed if duration and speed else None
    return ConversionProgress(seconds, percent, speed, remaining)


def run_transcode(command: list[str], *, duration: float | None,
                  cancel_event: threading.Event, on_progress: Callable[[ConversionProgress], None],
                  timeout: float | None = None) -> str:
    """Drain stderr continuously, coalesce progress, and always reap the owned process."""
    if cancel_event.is_set():
        raise FFmpegError("cancelled", "Conversion cancelled.")
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        raise FFmpegError("engine_start_failed", "Could not start FFmpeg.", str(exc)) from exc
    updates: queue.Queue = queue.Queue(maxsize=1)
    diagnostics = bytearray()

    def read_progress():
        values = {}
        for raw in iter(lambda: process.stdout.readline(8192), b""):
            key, separator, value = raw.decode("utf-8", errors="replace").strip().partition("=")
            if separator and key in {"out_time_us", "out_time", "speed", "progress"}:
                values[key] = value
            if key == "progress":
                progress = parse_progress(values, duration)
                try:
                    updates.get_nowait()
                except queue.Empty:
                    pass
                updates.put_nowait(progress)
                values = {}

    def read_errors():
        for chunk in iter(lambda: process.stderr.read(4096), b""):
            diagnostics.extend(chunk)
            del diagnostics[:-65536]

    readers = [threading.Thread(target=read_progress), threading.Thread(target=read_errors)]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    try:
        while True:
            if cancel_event.is_set():
                raise FFmpegError("cancelled", "Conversion cancelled.")
            if timeout is not None and time.monotonic() - started >= timeout:
                raise FFmpegError("timeout", "Video conversion timed out.")
            try:
                on_progress(updates.get(timeout=0.1))
            except queue.Empty:
                pass
            if process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait()
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()
    if cancel_event.is_set():
        raise FFmpegError("cancelled", "Conversion cancelled.")
    log = diagnostics.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise FFmpegError("encode_failed", "FFmpeg could not finish the conversion.", log)
    try:
        on_progress(updates.get_nowait())
    except queue.Empty:
        pass
    return log


def validate_output(source: MediaInfo, output: MediaInfo, options: VideoOptions) -> None:
    preset = validate_options(options)
    video = output.primary_video
    expected_codec = "vp9" if preset.id == "webm" else "h264"
    expected_audio = "opus" if preset.id == "webm" else "aac"
    expected_audio_count = int(source.primary_audio is not None and not options.mute)
    if (not video or video.codec_name != expected_codec or
            (video.width, video.height) != output_dimensions(source, options) or
            video.pixel_format != "yuv420p" or not math.isclose(video.rotation % 360, 0, abs_tol=0.001) or
            len(output.video_streams) != 1 or len(output.audio_streams) != expected_audio_count or
            (output.primary_audio and output.primary_audio.codec_name != expected_audio) or
            output.subtitle_streams or output.size_bytes <= 0 or preset.muxer not in output.format_names):
        raise FFmpegError("invalid_output", "The encoded output does not match the requested streams or format.")
    # A video can be shorter than another unselected stream. Compare selected streams only.
    durations = [source.primary_video.duration]
    if expected_audio_count:
        durations.append(source.primary_audio.duration)
    expected_duration = max((value for value in durations if value), default=source.duration)
    if expected_duration and (output.duration is None or
                              abs(output.duration - expected_duration) > max(0.5, expected_duration * 0.02)):
        raise FFmpegError("invalid_output", "The encoded output duration differs from the source.")


def convert_video(task: MediaTask, engine: EngineCapabilities, *,
                  cancel_event: threading.Event | None = None,
                  events: queue.Queue | None = None,
                  protected_sources: tuple[Path, ...] = ()) -> ConversionResult:
    """Return exactly one terminal result after all processes and temp files are closed."""
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    source = task.source.expanduser().resolve()
    destination = task.destination.expanduser().absolute()
    warnings: tuple[str, ...] = ()
    log = ""
    last_percent = 0.0

    def emit(state, progress=None, result=None):
        if events is not None:
            events.put(TaskEvent(task.batch_id, task.task_id, state, progress, result))

    def check_cancel():
        if cancel_event.is_set():
            raise FFmpegError("cancelled", "Conversion cancelled.")

    def progress(value):
        nonlocal last_percent
        if value.percent is not None:
            last_percent = max(last_percent, value.percent)
            value = replace(value, percent=last_percent)
        emit("running", value)

    try:
        check_cancel()
        preset = validate_options(task.options)
        if destination.suffix.lower() != preset.extension:
            raise FFmpegError("invalid_options", "Output extension must match the selected container.")
        with OutputTransaction(destination, (source, *protected_sources), task.options.overwrite) as output:
            emit("probing")
            media = probe_media(source, engine.paths, cancel_event=cancel_event)
            warnings = tuple(reason for condition, reason in (
                (len(media.video_streams) > 1, "Only the primary video stream is retained."),
                (len(media.audio_streams) > 1 and not task.options.mute, "Only the default/first audio track is retained."),
                (bool(media.subtitle_streams), "Subtitles are omitted."),
                (any(s.codec_type == "attachment" or s.is_attached_picture for s in media.streams), "Attachments and cover art are omitted."),
                (bool(media.primary_video.bits_per_raw_sample and media.primary_video.bits_per_raw_sample > 8), "Output uses 8-bit SDR color."),
            ) if condition)
            command = build_command(media, output.temporary, task.options, engine)
            check_cancel()
            emit("running", ConversionProgress(percent=0.0 if media.duration else None))
            log = run_transcode(command, duration=media.duration, cancel_event=cancel_event,
                                on_progress=progress, timeout=task.options.timeout)
            check_cancel()
            emit("validating")
            converted = probe_media(output.temporary, engine.paths, cancel_event=cancel_event)
            validate_output(media, converted, task.options)
            check_cancel()
            published = output.commit()
            converted = replace(converted, path=published)
        result = ConversionResult(task.task_id, task.batch_id, source, destination, "succeeded",
                                  media=converted, details=log, warnings=warnings)
    except FFmpegError as exc:
        state = "cancelled" if exc.code == "cancelled" else "failed"
        if state == "cancelled":
            emit("cancelling")
        result = ConversionResult(task.task_id, task.batch_id, source, destination, state,
                                  error_code=exc.code, error_message=str(exc), details=exc.details, warnings=warnings)
    except OSError as exc:
        result = ConversionResult(task.task_id, task.batch_id, source, destination, "failed",
                                  error_code="file_error", error_message="Could not read or write a media file.",
                                  details=str(exc), warnings=warnings)
    except Exception as exc:
        # Isolate unexpected per-file failures so a batch can still finish its other tasks.
        result = ConversionResult(task.task_id, task.batch_id, source, destination, "failed",
                                  error_code="internal_error", error_message="Unexpected conversion failure.",
                                  details=f"{type(exc).__name__}: {exc}", warnings=warnings)
    emit(result.state, ConversionProgress(percent=100.0) if result.state == "succeeded" else None, result)
    return result
