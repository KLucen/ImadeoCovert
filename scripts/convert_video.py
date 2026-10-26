"""Convert videos locally and print task events as JSON lines. Ctrl+C cancels the batch."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import queue
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ffmpeg_service import FFmpegError, discover_engine, inspect_engine  # noqa: E402
from media_models import VideoOptions  # noqa: E402
from task_runner import TaskRunner, make_tasks  # noqa: E402
from scripts.probe_media import encode  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--format", choices=["mp4", "mkv", "webm"], default="mp4")
    parser.add_argument("--quality", choices=["high", "balanced", "compact"], default="balanced")
    parser.add_argument("--speed", choices=["fast", "balanced", "slow"], default="balanced")
    parser.add_argument("--height", type=int, choices=[720, 1080])
    parser.add_argument("--mute", action="store_true")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing outputs only after verification")
    parser.add_argument("--timeout", type=float, help="Per-file encoding timeout in seconds")
    parser.add_argument("--engine-dir", type=Path)
    args = parser.parse_args()
    options = VideoOptions(args.format, args.quality, args.speed, args.height,
                           args.mute, args.overwrite, args.timeout)
    stop = threading.Event()
    events = queue.Queue()
    results, failures = [], []
    try:
        tasks = make_tasks(args.sources, args.output_dir, options)
        runner = TaskRunner(inspect_engine(discover_engine(args.engine_dir)))

        def work():
            try:
                results.extend(runner.run(tasks, cancel_event=stop, events=events))
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=work)
        worker.start()
        try:
            while worker.is_alive() or not events.empty():
                try:
                    event = events.get(timeout=0.1)
                    print(json.dumps(asdict(event), default=encode, ensure_ascii=True), flush=True)
                except queue.Empty:
                    pass
        except KeyboardInterrupt:
            stop.set()
        finally:
            # Also stop and reap on output errors (e.g. a closed stdout pipe).
            if worker.is_alive():
                stop.set()
            worker.join()
        while not events.empty():
            print(json.dumps(asdict(events.get()), default=encode, ensure_ascii=True), flush=True)
        if failures:
            raise failures[0]
        if any(result.state == "cancelled" for result in results):
            return 130
        return 0 if results and all(result.state == "succeeded" for result in results) else 1
    except FFmpegError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), "details": exc.details}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
