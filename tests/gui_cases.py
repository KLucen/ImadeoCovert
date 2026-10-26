"""Native GUI cases, each invoked in a fresh process by test_video_gui.py."""

from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import formatix
from ffmpeg_service import FFmpegError
from media_models import TaskEvent
from test_video_integration import engine, ffmpeg
from test_video_conversion_integration import sample


def pump(app, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "GUI operation did not finish"
        app.update()
        time.sleep(0.01)
    app.update()


@pytest.fixture
def app(tmp_path, monkeypatch, request):
    monkeypatch.setattr(formatix, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(formatix, "load_settings", lambda: {"check_updates": False, "lang": "zh"})
    monkeypatch.setattr(formatix, "_settings_error", None)
    theme = getattr(request, "param", "light")
    palette = formatix.THEMES[theme]
    monkeypatch.setattr(formatix, "ACTIVE_THEME", theme)
    monkeypatch.setattr(formatix, "_palette", palette)
    for key, value in palette.items():
        monkeypatch.setattr(formatix, key, value)
    errors = []
    root = formatix.App()
    root.withdraw()
    root.report_callback_exception = lambda *args: errors.append(args)
    main_thread = threading.get_ident()
    original_after = root.after
    def after(*args, **kwargs):
        assert threading.get_ident() == main_thread, "Tk called from worker"
        return original_after(*args, **kwargs)
    monkeypatch.setattr(root, "after", after)
    try:
        pump(root, lambda: not root._video_panel.busy)
        yield root
        assert not errors, errors
    finally:
        if not root._closing:
            root._video_panel.shutdown()
            root._video_panel.session.thread.join(timeout=10)
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()


@pytest.mark.integration
def test_missing_engine_disables_only_video_and_can_recover(app, engine, tmp_path):
    panel = app._video_panel
    panel.engine_dir = str(tmp_path / "missing-engine")
    panel.reload_engine()
    pump(app, lambda: not panel.busy)
    assert panel.engine is None
    assert str(panel.convert_btn["state"]) == "disabled"
    assert panel.engine_error.code == "engine_missing"
    app._switch_mode("image")
    assert app._mode == "image" and app._fmt.get() in formatix.FORMATS
    panel.default_engine()
    pump(app, lambda: not panel.busy)
    assert panel.engine is not None


@pytest.mark.integration
def test_real_gui_batch_retry_and_mode_isolation(app, engine, tmp_path, monkeypatch):
    good = sample(engine, tmp_path / "中文 视频.mp4")
    damaged = tmp_path / "broken.mp4"
    damaged.write_bytes(b"damaged input")
    ignored = tmp_path / "notes.txt"
    ignored.write_text("ignore")
    panel = app._video_panel
    app._switch_mode("video")
    panel.add_paths([good, damaged, good, ignored, tmp_path])
    pump(app, lambda: not panel.busy)
    assert len(panel.rows) == 2
    ids = list(panel.rows)
    assert panel.rows[ids[0]].media.primary_video.width == 128
    assert panel.rows[ids[1]].error is not None
    panel.values.update(preset="mkv", quality="high", speed="fast", max_height=720)
    panel.mute.set(True)
    panel.out_dir.set(str(tmp_path / "output"))
    panel.start()
    app._switch_mode("image")
    assert app._mode == "video" and app._batch_busy
    assert str(panel.combos["quality"]["state"]) == "disabled"
    pump(app, lambda: not panel.busy)
    assert [panel.rows[key].state for key in ids] == ["succeeded", "failed"]
    assert not panel.rows[ids[0]].result.media.audio_streams
    assert panel.rows[ids[0]].destination.suffix == ".mkv"
    assert not app._batch_busy
    # Repaired input is retried, while the successful file is kept untouched.
    destination = panel.rows[ids[0]].destination
    original = destination.read_bytes()
    damaged.write_bytes(good.read_bytes())
    previous_batch = panel.batch_id
    panel.start(retry=True)
    assert panel.batch_id != previous_batch
    panel._task_event(TaskEvent(previous_batch, ids[1], "failed"))
    pump(app, lambda: not panel.busy)
    assert all(row.state == "succeeded" for row in panel.rows.values())
    assert destination.read_bytes() == original
    opened = []
    monkeypatch.setattr(panel, "open_path", opened.append)
    panel.result_tree.selection_set(ids[1])
    panel.play()
    assert opened == [str(panel.rows[ids[1]].destination)]
    app._switch_mode("image")
    assert not app._files
    app._switch_mode("video")
    assert len(panel.rows) == 2
    app._lang = "en"
    app._update_ui_strings()
    assert panel.values["quality"] == "high"
    assert panel.combos["quality"].get() == "High"
    assert panel.result_tree.item(ids[1], "values")[0] == "Complete"
    app._save_settings()
    saved = json.loads(Path(formatix.SETTINGS_FILE).read_text())
    assert saved["image"]["quality"] == app._qual.get()
    assert saved["video"]["quality"] == "high"


@pytest.mark.integration
def test_cancel_clear_waits_and_restart_uses_fresh_identity(app, engine, tmp_path):
    source = sample(engine, tmp_path / "long.mp4", size="640x360", duration="12")
    panel = app._video_panel
    app._switch_mode("video")
    panel.add_paths([source, tmp_path / "missing.mp4"])
    pump(app, lambda: not panel.busy)
    panel.values.update(preset="webm", speed="slow")
    panel.out_dir.set(str(tmp_path / "out"))
    panel.start()
    pump(app, lambda: any(row.state == "running" for row in panel.rows.values()))
    old_id, old_stop, row_id = panel.batch_id, panel.session.stop, next(iter(panel.rows))
    panel.clear()
    assert panel.busy and panel.pending_clear and app._batch_busy
    panel.start()
    assert panel.batch_id == old_id
    pump(app, lambda: not panel.busy)
    assert not panel.rows and old_stop.is_set() and not panel.session.busy
    assert not list((tmp_path / "out").glob(".imadeo-*"))
    panel._task_event(TaskEvent(old_id, row_id, "succeeded"))
    assert not panel.rows
    panel.add_paths([source])
    pump(app, lambda: not panel.busy)
    panel.values.update(preset="mp4", speed="fast")
    panel.start()
    assert panel.session.stop is not old_stop
    pump(app, lambda: not panel.busy)
    assert next(iter(panel.rows.values())).state == "succeeded"


@pytest.mark.integration
def test_close_reaps_active_video_processes(app, engine, tmp_path, monkeypatch):
    source = sample(engine, tmp_path / "close.mp4", size="640x360", duration="12")
    panel = app._video_panel
    app._switch_mode("video")
    panel.add_paths([source])
    pump(app, lambda: not panel.busy)
    import video_converter
    original_popen = video_converter.subprocess.Popen
    children = []
    def record(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(video_converter.subprocess, "Popen", record)
    panel.values.update(preset="webm", speed="slow")
    panel.out_dir.set(str(tmp_path / "out"))
    panel.start()
    pump(app, lambda: any(row.state == "running" for row in panel.rows.values()))
    app._request_close()
    deadline = time.monotonic() + 10
    while panel.session.busy:
        assert time.monotonic() < deadline
        app.update()
        time.sleep(0.01)
    # Allow the close callback to destroy the root after worker reaping.
    time.sleep(0.1)
    app.update()
    assert children and all(child.poll() is not None for child in children)
    assert not list((tmp_path / "out").glob(".imadeo-*"))


def test_image_conversion_still_works_and_locks_mode(app, tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    Image.new("RGB", (64, 48), "red").save(source)
    app._fmt.set("JPEG")
    app._out_dir.set(str(tmp_path))
    app._drop(SimpleNamespace(data=app.tk.call("list", str(source))))
    app._start()
    assert app._running and app._batch_busy
    app._switch_mode("video")
    assert app._mode == "image"
    pump(app, lambda: not app._running)
    assert app._results[0][1] is True
    with Image.open(app._results[0][0]) as image:
        assert image.size == (64, 48) and image.format == "JPEG"
    assert not app._batch_busy


@pytest.mark.parametrize("app", ["light", "dark"], indirect=True)
def test_minimum_window_layout_and_language_values(app):
    panel = app._video_panel
    assert panel.cget("bg") == formatix.THEMES[app._theme]["BG"]
    app._switch_mode("video")
    app.geometry("1150x640")
    app.deiconify()
    for language in ("zh", "en", "de"):
        app._lang = language
        app._update_ui_strings()
        app.update_idletasks()
        assert panel.current_label.winfo_y() + panel.current_label.winfo_height() <= panel.winfo_height()
        assert panel.current_label.winfo_ismapped()
        assert panel.values["quality"] == "balanced"


def test_image_clear_waits_for_existing_worker_before_mode_unlock(app, tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    Image.new("RGB", (64, 48), "blue").save(source)
    entered, release = threading.Event(), threading.Event()
    original = formatix.convert_one
    def slow_convert(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(formatix, "convert_one", slow_convert)
    app._fmt.set("JPEG")
    app._out_dir.set(str(tmp_path))
    app._drop(SimpleNamespace(data=app.tk.call("list", str(source))))
    try:
        app._start()
        pump(app, entered.is_set)
        app._clear()
        app._switch_mode("video")
        assert app._running and app._batch_busy and app._mode == "image"
    finally:
        release.set()
    pump(app, lambda: not app._running)
    assert not app._files and not app._results and not app._batch_busy


@pytest.mark.integration
def test_capability_filter_respects_mute_and_can_recover_options(app, engine):
    panel = app._video_panel
    original_engine = panel.engine
    panel.engine = replace(original_engine, encoders=frozenset({"libx264"}))
    panel._refresh_presets()
    assert not panel.choices["preset"]
    assert str(panel.convert_btn["state"]) == "disabled"
    panel.mute.set(True)
    panel._mute_changed()
    assert [key for key, label in panel.choices["preset"]] == ["mp4", "mkv"]
    panel.engine = original_engine
    panel._refresh_presets()
    assert len(panel.choices["preset"]) == 3


def test_update_identity_and_development_version():
    import app_metadata
    import update
    assert update.GITHUB_REPO == "KLucen/ImadeoCovert"
    assert formatix.APP_NAME == app_metadata.APP_NAME == "ImadeoCovert"
    assert update.is_newer("v1.18.0", app_metadata.VERSION)
    assert not update.is_newer("v1.17.4", app_metadata.VERSION)
