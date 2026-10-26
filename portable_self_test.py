"""Exercise the shipped GUI and codecs in a new, isolated output directory.

Invoked only through an explicit --self-test argument; never on normal startup.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

from app_metadata import VERSION


def run(directory: Path) -> int:
    directory = directory.expanduser().resolve()
    # All diagnostic writes are confined to a newly-created directory.
    directory.mkdir(parents=True, exist_ok=False)
    report = {"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)),
              "executable": sys.executable, "cwd": str(Path.cwd()), "checks": [], "success": False}
    app = None
    errors = []
    original_connect = socket.socket.connect

    def offline(*args, **kwargs):
        raise OSError("Network is disabled during the portable self-test")

    def check(name, condition, detail=None):
        report["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"Failed: {name}: {detail}")

    def pump(predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError(str(errors))
            if time.monotonic() > deadline:
                raise TimeoutError("GUI self-test operation timed out")
            app.update()
            time.sleep(0.01)
        app.update()
        if errors:
            raise RuntimeError(str(errors))

    try:
        socket.socket.connect = offline
        from PIL import Image
        import formatix
        from ffmpeg_service import probe_media
        formatix.SETTINGS_FILE = str(directory / "settings.json")
        formatix.load_settings = lambda: {"lang": "en", "check_updates": False}
        formatix._settings_error = None
        app = formatix.App()
        app.withdraw()
        app.report_callback_exception = lambda *args: errors.append(str(args))
        panel = app._video_panel
        pump(lambda: not panel.busy)
        check("bundled_engine", panel.engine is not None, str(panel.engine_error or ""))
        engine = panel.engine
        check("engine_is_bundled", engine.paths.source == "bundled", str(engine.paths.ffmpeg))
        check("drag_and_drop", formatix.HAS_DND and bool(getattr(app, "TkdndVersion", None)))
        check("icon", bool(getattr(app, "_icon_refs", None)))
        check("optional_image_codecs", formatix.HEIF_AVAILABLE and formatix.AVIF_AVAILABLE and formatix.SVG_AVAILABLE)
        report["engine"] = {"path": str(engine.paths.ffmpeg), "version": engine.version}

        # Run image conversion through the original GUI, including native wheels.
        source_image = directory / "图片 (source).png"
        Image.new("RGB", (64, 48), (70, 125, 200)).save(source_image)
        app._drop(SimpleNamespace(data=app.tk.call("list", str(source_image))))
        for output_format in ("JPEG", "WEBP", "AVIF", "HEIC"):
            target = directory / output_format
            target.mkdir()
            app._fmt.set(output_format)
            app._out_dir.set(str(target))
            app._start()
            pump(lambda: not app._running)
            check(f"image_{output_format}", len(app._results) == 1 and app._results[0][1])
            with Image.open(app._results[0][0]) as converted:
                check(f"image_{output_format}_size", converted.size == (64, 48))
            if output_format == "JPEG":
                app._tv_src.selection_set(str(source_image))
                app._tv_dst.selection_set(app._tv_dst.get_children()[0])
                app._open_compare()
                app.update()
                check("image_compare_window", app._compare_win is not None and app._compare_win.winfo_exists())
                app._compare_win.destroy()
                app._compare_win = None
        app._clear()
        svg = directory / "矢量.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48" fill="blue"/></svg>', encoding="utf-8")
        app._drop(SimpleNamespace(data=app.tk.call("list", str(svg))))
        app._fmt.set("PNG")
        app._out_dir.set(str(directory))
        app._start()
        pump(lambda: not app._running)
        check("svg_render", bool(app._results) and app._results[0][1])

        def generate(path, audio):
            args = [str(engine.paths.ffmpeg), "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                    "testsrc2=size=128x72:rate=15:duration=1"]
            if audio:
                args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac"]
            args += ["-c:v", "libx264", "-preset", "ultrafast", str(path)]
            subprocess.run(args, check=True, capture_output=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

        source = directory / "中文 audio (1).mp4"
        silent = directory / "silent.mp4"
        generate(source, True)
        generate(silent, False)
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        app._switch_mode("video")
        panel.add_paths([source, silent])
        pump(lambda: not panel.busy)
        for preset in ("mp4", "mkv", "webm"):
            panel.values.update(preset=preset, speed="fast")
            panel.out_dir.set(str(directory / preset))
            panel.start()
            pump(lambda: not panel.busy, 45)
            check(f"video_{preset}", all(row.state == "succeeded" for row in panel.rows.values()),
                  [str(row.result.error_message) for row in panel.rows.values() if row.result])
            for row in panel.rows.values():
                converted = probe_media(row.destination, engine.paths)
                check(f"{preset}_{row.path.stem}_audio", bool(converted.audio_streams) == (row.path == source))
                subprocess.run([str(engine.paths.ffmpeg), "-v", "error", "-xerror", "-i", str(row.destination),
                                "-f", "null", "-"], check=True, capture_output=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        check("original_preserved", hashlib.sha256(source.read_bytes()).hexdigest() == original)

        # Exercise cancellation and restart on the shipped TaskRunner.
        from media_models import MediaTask, VideoOptions
        from task_runner import TaskRunner
        import queue
        import threading
        stop, events, results = threading.Event(), queue.Queue(), []
        task = MediaTask(source, directory / "cancelled.webm", VideoOptions(preset="webm", speed="slow"))
        worker = threading.Thread(target=lambda: results.extend(TaskRunner(engine).run((task,), cancel_event=stop, events=events)))
        worker.start()
        started = time.monotonic()
        try:
            while worker.is_alive():
                event = events.get(timeout=5)
                if event.state == "running":
                    stop.set()
                    break
        finally:
            stop.set()
            worker.join(timeout=10)
        check("cancelled_and_reaped", not worker.is_alive() and bool(results) and results[0].state == "cancelled",
              {"seconds": round(time.monotonic() - started, 3)})
        check("cancelled_output_absent", not task.destination.exists())
        check("temporary_files_cleaned", not list(directory.rglob(".imadeo-*")))
        check("no_tk_callback_errors", not errors, errors)
        report["success"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if app is not None:
            try:
                app._stop_requested = True
                app._video_panel.shutdown()
                if app._video_panel.session.thread:
                    app._video_panel.session.thread.join(timeout=10)
                if app._image_worker:
                    app._image_worker.join(timeout=10)
                for callback in app.tk.call("after", "info"):
                    app.after_cancel(callback)
                app.destroy()
            except Exception:
                report["cleanup_error"] = traceback.format_exc()
                report["success"] = False
        socket.socket.connect = original_connect
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["success"] else 1
