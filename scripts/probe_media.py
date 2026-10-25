"""Print engine capabilities and optional video metadata as JSON (no conversion)."""

import argparse
from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ffmpeg_service import FFmpegError, discover_engine, inspect_engine, probe_media  # noqa: E402
from media_models import VIDEO_PRESETS  # noqa: E402


def encode(value):
    if isinstance(value, (Path, Fraction)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value).__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path)
    parser.add_argument("--engine-dir", type=Path)
    parser.add_argument("--require-presets", action="store_true")
    args = parser.parse_args()
    try:
        engine = inspect_engine(discover_engine(args.engine_dir))
        presets = {preset.id: {"available": not engine.missing_for(preset),
                               "missing": engine.missing_for(preset)} for preset in VIDEO_PRESETS}
        result = {"engine": asdict(engine), "presets": presets}
        if args.file:
            info = probe_media(args.file, engine.paths)
            result["media"] = asdict(info)
            result["selected_video_index"] = info.primary_video.index
            result["selected_audio_index"] = info.primary_audio.index if info.primary_audio else None
        print(json.dumps(result, default=encode, ensure_ascii=True, indent=2))
        return 2 if args.require_presets and any(not p["available"] for p in presets.values()) else 0
    except FFmpegError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), "details": exc.details},
                         ensure_ascii=True, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
