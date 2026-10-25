"""GUI-independent media metadata for the video conversion pipeline.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    average_frame_rate: Fraction | None = None
    nominal_frame_rate: Fraction | None = None
    sample_aspect_ratio: Fraction | None = None
    rotation: float = 0.0
    pixel_format: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    bits_per_raw_sample: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    language: str | None = None
    is_default: bool = False
    is_attached_picture: bool = False
    side_data_types: tuple[str, ...] = ()
    alpha_mode: bool = False

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in {"smpte2084", "arib-std-b67"} or any(
            token in kind.lower()
            for kind in self.side_data_types
            for token in ("dovi", "dolby vision", "mastering display", "hdr10")
        )

    @property
    def has_alpha(self) -> bool:
        pixel_format = self.pixel_format or ""
        return self.alpha_mode or pixel_format.startswith(
            ("yuva", "gbrap", "rgba", "bgra", "argb", "abgr", "ya")
        )

    @property
    def display_size(self) -> tuple[int, int] | None:
        """Approximate square-pixel dimensions, accounting for quarter turns."""
        if not self.width or not self.height:
            return None
        width = max(1, round(self.width * (self.sample_aspect_ratio or 1)))
        if round(self.rotation) % 360 in (90, 270):
            return self.height, width
        return width, self.height


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    format_names: tuple[str, ...]
    duration: float | None
    size_bytes: int
    streams: tuple[StreamInfo, ...]

    @property
    def video_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(s for s in self.streams if s.codec_type == "video"
                     and not s.is_attached_picture and s.width and s.height)

    @property
    def audio_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(s for s in self.streams if s.codec_type == "audio")

    @property
    def subtitle_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(s for s in self.streams if s.codec_type == "subtitle")

    @property
    def primary_video(self) -> StreamInfo | None:
        return self._preferred(self.video_streams)

    @property
    def primary_audio(self) -> StreamInfo | None:
        return self._preferred(self.audio_streams)

    @staticmethod
    def _preferred(streams: tuple[StreamInfo, ...]) -> StreamInfo | None:
        return min(streams, key=lambda s: (not s.is_default, s.index), default=None)


@dataclass(frozen=True)
class VideoPreset:
    id: str
    extension: str
    muxer: str
    video_encoder: str
    audio_encoder: str


VIDEO_PRESETS = (
    VideoPreset("mp4", ".mp4", "mp4", "libx264", "aac"),
    VideoPreset("mkv", ".mkv", "matroska", "libx264", "aac"),
    VideoPreset("webm", ".webm", "webm", "libvpx-vp9", "libopus"),
)
