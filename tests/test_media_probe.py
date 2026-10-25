from fractions import Fraction
from pathlib import Path

import pytest

from ffmpeg_service import FFmpegError, parse_probe


def video(index=0, **kwargs):
    return {"index": index, "codec_type": "video", "codec_name": "h264", "width": 1920,
            "height": 1080, "avg_frame_rate": "30000/1001", "r_frame_rate": "30/1", **kwargs}


def parse(streams, **format_info):
    return parse_probe({"streams": streams, "format": format_info}, Path("sample.mp4"), size_bytes=123)


def test_preserves_exact_rates_rotation_and_aspect_ratio():
    info = parse([video(sample_aspect_ratio="4:3", tags={"rotate": "180"},
                        side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}])], duration="2.5")
    stream = info.primary_video
    assert stream.average_frame_rate == Fraction(30000, 1001)
    assert stream.nominal_frame_rate == 30
    assert stream.rotation == 270
    assert stream.display_size == (1080, 2560)
    assert info.duration == 2.5 and info.primary_audio is None


def test_excludes_cover_art_and_selects_default_tracks():
    info = parse([video(disposition={"attached_pic": 1, "default": 1}), video(1),
                  video(4, disposition={"default": 1}),
                  {"index": 2, "codec_type": "audio", "codec_name": "aac"},
                  {"index": 3, "codec_type": "audio", "codec_name": "aac",
                   "disposition": {"default": 1}, "tags": {"language": "zho"}},
                  {"index": 5, "codec_type": "subtitle", "codec_name": "subrip"}])
    assert info.primary_video.index == 4 and len(info.video_streams) == 2
    assert info.primary_audio.index == 3 and info.primary_audio.language == "zho"
    assert len(info.subtitle_streams) == 1


@pytest.mark.parametrize("value", ["N/A", "0/0", "nan", "inf", "-1", None])
def test_optional_invalid_numbers_are_unknown(value):
    info = parse([video(avg_frame_rate=value, duration=value, sample_aspect_ratio=value)], duration=value)
    assert info.duration is None
    assert info.primary_video.average_frame_rate is None
    assert info.primary_video.sample_aspect_ratio is None


def test_duration_falls_back_to_stream_time_base():
    info = parse([video(duration_ts=60000, time_base="1/30000")], duration="N/A")
    assert info.duration == 2.0


@pytest.mark.parametrize("data", [{}, {"streams": None}, {"streams": [None]},
                                  {"streams": [video(), video()]}, {"streams": [video(index=-1)]}])
def test_malformed_structures_have_explicit_error(data):
    with pytest.raises(FFmpegError) as exc:
        parse_probe(data, Path("invalid"), size_bytes=0)
    assert exc.value.code == "invalid_probe"


@pytest.mark.parametrize("streams", [[], [video(disposition={"attached_pic": 1})],
                                      [{"index": 0, "codec_type": "audio"}], [video(width=0)]])
def test_requires_real_video(streams):
    with pytest.raises(FFmpegError) as exc:
        parse(streams)
    assert exc.value.code == "no_video"


@pytest.mark.parametrize("metadata", [
    {"color_transfer": "smpte2084"}, {"color_transfer": "arib-std-b67"},
    {"side_data_list": [{"side_data_type": "DOVI configuration record"}]},
])
def test_hdr_and_dolby_vision_are_identifiable(metadata):
    assert parse([video(**metadata)]).primary_video.is_hdr


def test_alpha_and_bit_depth_are_preserved():
    stream = parse([video(pix_fmt="yuva420p10le", bits_per_raw_sample="10")]).primary_video
    assert stream.has_alpha and stream.bits_per_raw_sample == 10
