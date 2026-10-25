"""Generated image fixtures establish the existing converter's behavior."""

import hashlib
from pathlib import Path

import pytest
from PIL import Image

import converter


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "图片 sample (1).png"
    image = Image.new("RGBA", (96, 64), (40, 100, 200, 255))
    image.paste((255, 0, 0, 0), (0, 0, 20, 20))
    image.save(path)
    return path


def convert(source, tmp_path, fmt="PNG", mode="no_change", width=0, height=0, **kwargs):
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    return converter.convert_one(str(source), str(output), fmt, f"result.{fmt.lower()}",
                                 85, mode, width, height, f"{fmt}:{mode}:{width}:{height}",
                                 resize_key=mode, **kwargs)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "BMP", "TIFF", "ICO", "AVIF", "HEIC"])
def test_supported_outputs_are_readable_without_changing_source(source, tmp_path, fmt):
    if fmt not in converter.FORMATS:
        pytest.skip(f"Optional format unavailable: {fmt}")
    original = hashlib.sha256(source.read_bytes()).digest()
    result = convert(source, tmp_path, fmt)
    assert result["success"], result.get("error_msg")
    with Image.open(result["out_path"]) as output:
        output.load()
        if fmt != "ICO":
            assert output.size == (96, 64)
        if fmt == "PNG":
            assert output.getpixel((5, 5))[3] == 0
        if fmt == "JPEG":
            assert all(channel > 240 for channel in output.getpixel((5, 5)))
    assert result["f_size"] > 0
    assert not Path(result["out_path"] + ".tmp").exists()
    assert hashlib.sha256(source.read_bytes()).digest() == original


@pytest.mark.parametrize("mode,width,height,expected", [
    ("prop_width", 48, 0, (48, 32)), ("prop_height", 0, 32, (48, 32)),
    ("smart_crop", 32, 32, (32, 32)), ("custom", 40, 24, (40, 24)),
])
def test_resize_modes(source, tmp_path, mode, width, height, expected):
    result = convert(source, tmp_path, mode=mode, width=width, height=height)
    assert result["success"], result.get("error_msg")
    with Image.open(result["out_path"]) as output:
        assert output.size == expected


def test_svg_rasterization(tmp_path):
    if not converter.SVG_AVAILABLE:
        pytest.skip("SVG renderer not installed")
    source = tmp_path / "sample.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
                      '<rect width="80" height="40" fill="#336699"/></svg>', encoding="utf-8")
    result = convert(source, tmp_path)
    assert result["success"], result.get("error_msg")
    with Image.open(result["out_path"]) as output:
        assert output.size == (80, 40)
        assert output.getpixel((30, 20))[:3] == (51, 102, 153)


def test_invalid_image_preserves_existing_output(tmp_path):
    source = tmp_path / "broken.png"
    source.write_bytes(b"not an image")
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "result.png"
    existing.write_bytes(b"previous output")
    result = convert(source, tmp_path)
    assert not result["success"] and result["error_msg"]
    assert existing.read_bytes() == b"previous output"


def test_naming_avoids_disk_and_batch_conflicts(tmp_path):
    (tmp_path / "image.png").touch()
    reserved = set()
    assert converter.generate_unique_filename(str(tmp_path), "image", ".png", reserved) == "image_1.png"
    assert converter.generate_unique_filename(str(tmp_path), "IMAGE", ".png", reserved) == "IMAGE_2.png"
    assert converter.render_filename_template("photo", 3, "number", []) == "photo_003"


def test_cache_reuses_identical_conversion_and_recovers_deleted_output(source, tmp_path):
    cache = {}
    first = convert(source, tmp_path, cache=cache)
    second = convert(source, tmp_path, cache=cache)
    assert first["success"] and second["cached"]
    Path(first["out_path"]).unlink()
    third = convert(source, tmp_path, cache=cache)
    assert third["success"] and not third["cached"]


def test_target_size_mode(source, tmp_path):
    result = convert(source, tmp_path, fmt="JPEG", quality_mode="size", target_bytes=8192)
    assert result["success"] and 0 < result["f_size"] <= 8192
