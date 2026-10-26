"""Versioned, atomic settings with a read-only legacy migration.

The original image UI uses a flat in-memory mapping; this adapter keeps that
interface while separating persisted general, image and video preferences.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
from pathlib import Path
import tempfile

SETTINGS_PATH = Path.home() / ".imadeocovert_settings.json"
LEGACY_PATH = Path.home() / ".formatix_image_converter_settings.json"
GENERAL = {"lang", "theme", "remember_settings", "check_updates", "last_update_check", "engine_dir"}
IMAGE = {"fmt", "quality", "quality_mode", "target_size_val", "target_size_unit",
         "resize_mode_key", "out_dir", "filename_preset", "filename_tokens"}
VIDEO = {"preset", "quality", "speed", "max_height", "mute", "out_dir"}


class SettingsError(ValueError):
    pass


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: expected a JSON object")
    return data


def _document(path):
    data = _read(path)
    if (type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or any(not isinstance(data.get(key), dict) for key in ("general", "image", "video"))):
        raise SettingsError(f"{path}: unsupported settings schema; file preserved")
    return data


def _clean(values, keys):
    result = {}
    for key in keys & values.keys():
        value = values[key]
        if key in {"remember_settings", "check_updates", "mute"}:
            valid = type(value) is bool
        elif key == "quality" and "fmt" in keys:
            valid = type(value) is int and 1 <= value <= 100
        elif key in {"target_size_val", "last_update_check"}:
            valid = type(value) in (int, float) and 0 <= value < 1e12
        elif key == "max_height":
            valid = value is None or type(value) is int and value in (720, 1080)
        elif key == "filename_tokens":
            valid = isinstance(value, list) and all(isinstance(t, dict) for t in value)
        else:
            valid = isinstance(value, str)
        if valid:
            result[key] = value
    return result


def load(path=SETTINGS_PATH, legacy=LEGACY_PATH):
    path = Path(path)
    if path.exists():
        data = _document(path)
        general = _clean(data["general"], GENERAL)
        if general.get("remember_settings", True):
            return {**general, **_clean(data["image"], IMAGE), "video": _clean(data["video"], VIDEO)}
        return general
    if legacy is not None and Path(legacy).exists():
        data = _read(legacy)
        general = _clean(data, GENERAL)
        return {**general, **(_clean(data, IMAGE) if general.get("remember_settings", True) else {})}
    return {}


def save(values, path=SETTINGS_PATH):
    path = Path(path)
    # A malformed/newer file is never silently overwritten, including when load failed.
    if path.exists():
        _document(path)
    remember = values.get("remember_settings", True)
    data = {"schema_version": 1, "general": _clean(values, GENERAL),
            "image": _clean(values, IMAGE) if remember else {},
            "video": _clean(values.get("video", {}), VIDEO) if remember else {}}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
