"""Discover, inspect and probe a local FFmpeg toolchain without importing Tk.

All public operations are synchronous: GUI callers must use a worker thread.
No downloads, settings changes, or process launches happen at import time.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from media_models import MediaInfo, StreamInfo, VideoPreset


class FFmpegError(RuntimeError):
    """An actionable toolchain/probe failure; code is stable for UI translation."""

    def __init__(self, code: str, message: str, details: str = ""):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class EnginePaths:
    ffmpeg: Path
    ffprobe: Path
    source: str


@dataclass(frozen=True)
class EngineCapabilities:
    paths: EnginePaths
    version: str
    configuration: str
    encoders: frozenset[str]
    muxers: frozenset[str]

    def missing_for(self, preset: VideoPreset) -> tuple[str, ...]:
        missing = [f"encoder:{name}" for name in
                   (preset.video_encoder, preset.audio_encoder)
                   if name not in self.encoders]
        if preset.muxer not in self.muxers:
            missing.append(f"muxer:{preset.muxer}")
        return tuple(missing)


def discover_engine(configured_dir: str | Path | None = None, *,
                    app_root: Path | None = None,
                    allow_path: bool | None = None) -> EnginePaths:
    """Explicit directory, bundled pair, then PATH in source/developer mode.

    A broken explicit or bundled installation is reported, not silently replaced.
    Both executables must belong to the same directory.
    """
    suffix = ".exe" if os.name == "nt" else ""

    def pair(directory: Path, source: str) -> EnginePaths:
        directory = directory.expanduser().resolve()
        paths = EnginePaths(directory / f"ffmpeg{suffix}",
                            directory / f"ffprobe{suffix}", source)
        if not all(p.is_file() for p in (paths.ffmpeg, paths.ffprobe)):
            raise FFmpegError("engine_missing", "FFmpeg and FFprobe are required in the same directory.",
                              str(directory))
        return paths

    if configured_dir is not None:
        return pair(Path(configured_dir), "configured")
    frozen = bool(getattr(sys, "frozen", False))
    # Portable builds keep the engine beside the EXE, outside PyInstaller's
    # private _internal runtime. Never depend on the launch working directory.
    root = app_root if app_root is not None else (
        Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent)
    bundled = root / "tools" / "ffmpeg"
    if bundled.exists():
        return pair(bundled, "bundled")
    if allow_path is None:
        allow_path = not frozen
    if allow_path:
        found = shutil.which(f"ffmpeg{suffix}")
        if found:
            return pair(Path(found).resolve().parent, "path")
    raise FFmpegError("engine_missing", "No FFmpeg/FFprobe pair was found.")


def _run(args: list[str], *, timeout: float,
         cancel_event: threading.Event | None = None) -> str:
    """Bounded probe/inspection commands; communicate drains both pipes.

    Retrying communicate after a timeout preserves its buffered output. Cancellation
    and deadlines always terminate and reap the process before returning.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if cancel_event is not None and cancel_event.is_set():
        raise FFmpegError("cancelled", "Media inspection was cancelled.")
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        raise FFmpegError("engine_start_failed", "Could not start the media tool.", str(exc)) from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise FFmpegError("cancelled", "Media inspection was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FFmpegError("timeout", "Media inspection timed out.")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    if process.returncode:
        raise FFmpegError("process_failed", "The media tool could not read this input or execute the command.",
                          stderr.decode("utf-8", errors="replace")[-8192:])
    return stdout.decode("utf-8", errors="replace")


def _version(text: str, tool: str) -> tuple[str, str]:
    match = re.search(rf"^{tool} version (\S+)", text, re.MULTILINE)
    config = re.search(r"^configuration:\s*(.*)$", text, re.MULTILINE)
    if not match or not config:
        raise FFmpegError("invalid_engine", f"Unrecognized {tool} version output.")
    return match.group(1), " ".join(config.group(1).split())


def inspect_engine(paths: EnginePaths, *, timeout: float = 10,
                   cancel_event: threading.Event | None = None) -> EngineCapabilities:
    """Compare build identities and query actual encoders and muxers."""
    def run(executable: Path, *options: str) -> str:
        return _run([str(executable), *options], timeout=timeout, cancel_event=cancel_event)

    version, config = _version(run(paths.ffmpeg, "-version"), "ffmpeg")
    probe_version, probe_config = _version(run(paths.ffprobe, "-version"), "ffprobe")
    if (version, config) != (probe_version, probe_config):
        raise FFmpegError("engine_mismatch", "FFmpeg and FFprobe must come from the same build.")
    encoders = frozenset(re.findall(
        r"^\s*[VAS][A-Z.]{5}\s+([A-Za-z0-9_][A-Za-z0-9_.-]*)\s",
        run(paths.ffmpeg, "-hide_banner", "-encoders"), re.MULTILINE))
    muxers = frozenset(name for names in re.findall(
        r"^\s*E\s+([A-Za-z0-9_][A-Za-z0-9_,.-]*)\s",
        run(paths.ffmpeg, "-hide_banner", "-muxers"), re.MULTILINE)
        for name in names.split(","))
    if not encoders or not muxers:
        raise FFmpegError("invalid_engine", "The tool returned no usable encoder or muxer information.")
    return EngineCapabilities(paths, version, config, encoders, muxers)


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _positive_int(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number > 0 and number.is_integer() else None


def _duration(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _ratio(value: object) -> Fraction | None:
    try:
        ratio = Fraction(str(value).replace(":", "/"))
        return ratio if ratio > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value not in {"", "N/A", "unknown"} else None


def parse_probe(data: object, path: Path, *, size_bytes: int) -> MediaInfo:
    """Parse optional metadata defensively, rejecting invalid structure/video."""
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
        raise FFmpegError("invalid_probe", "FFprobe returned invalid media metadata.")
    streams = []
    seen = set()
    for raw in data["streams"]:
        if not isinstance(raw, dict):
            raise FFmpegError("invalid_probe", "FFprobe returned an invalid stream.")
        index = raw.get("index")
        if type(index) is not int or index < 0 or index in seen:
            raise FFmpegError("invalid_probe", "FFprobe returned invalid stream indices.")
        seen.add(index)
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        disposition = raw.get("disposition") if isinstance(raw.get("disposition"), dict) else {}
        side = raw.get("side_data_list") if isinstance(raw.get("side_data_list"), list) else []
        side = [item for item in side if isinstance(item, dict)]
        rotation = _number(tags.get("rotate")) or 0.0
        for item in side:
            angle = _number(item.get("rotation"))
            if angle is not None:
                rotation = angle
                break
        duration = _duration(raw.get("duration"))
        if duration is None:
            ticks, base = _duration(raw.get("duration_ts")), _ratio(raw.get("time_base"))
            if ticks is not None and base is not None:
                duration = _duration(ticks * float(base))
        streams.append(StreamInfo(
            index=index, codec_type=_text(raw.get("codec_type")) or "unknown",
            codec_name=_text(raw.get("codec_name")) or "unknown", duration=duration,
            width=_positive_int(raw.get("width")), height=_positive_int(raw.get("height")),
            average_frame_rate=_ratio(raw.get("avg_frame_rate")),
            nominal_frame_rate=_ratio(raw.get("r_frame_rate")),
            sample_aspect_ratio=_ratio(raw.get("sample_aspect_ratio")), rotation=rotation % 360,
            pixel_format=_text(raw.get("pix_fmt")), color_space=_text(raw.get("color_space")),
            color_transfer=_text(raw.get("color_transfer")), color_primaries=_text(raw.get("color_primaries")),
            bits_per_raw_sample=_positive_int(raw.get("bits_per_raw_sample")),
            channels=_positive_int(raw.get("channels")), sample_rate=_positive_int(raw.get("sample_rate")),
            language=_text(tags.get("language")), is_default=str(disposition.get("default")) == "1",
            is_attached_picture=str(disposition.get("attached_pic")) == "1",
            side_data_types=tuple(str(s.get("side_data_type", "")) for s in side),
            alpha_mode=str(tags.get("alpha_mode")) == "1",
        ))
    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    names = _text(format_info.get("format_name")) or ""
    duration = _duration(format_info.get("duration"))
    if duration is None:
        duration = max((s.duration for s in streams if s.duration is not None
                        and s.codec_type in {"video", "audio"} and not s.is_attached_picture), default=None)
    info = MediaInfo(path, tuple(n for n in names.split(",") if n), duration, size_bytes, tuple(streams))
    if not info.video_streams:
        raise FFmpegError("no_video", "No playable video stream was found (cover art does not count).")
    return info


def probe_media(path: str | Path, engine: EnginePaths, *, timeout: float = 15,
                cancel_event: threading.Event | None = None) -> MediaInfo:
    source = Path(path).expanduser().resolve()
    try:
        if not source.is_file():
            raise OSError("Not a regular file")
        size_bytes = source.stat().st_size
    except OSError as exc:
        raise FFmpegError("input_missing", "The input file cannot be accessed.", str(source)) from exc
    output = _run([str(engine.ffprobe), "-v", "error", "-show_format", "-show_streams",
                   "-of", "json", str(source)], timeout=timeout, cancel_event=cancel_event)
    try:
        data = json.loads(output)
    except (ValueError, RecursionError) as exc:
        raise FFmpegError("invalid_probe", "FFprobe returned invalid JSON.") from exc
    return parse_probe(data, source, size_bytes=size_bytes)
