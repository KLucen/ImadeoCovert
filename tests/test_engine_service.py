import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import ffmpeg_service as service
from media_models import VIDEO_PRESETS


def make_pair(directory):
    directory.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("ffmpeg", "ffprobe"):
        (directory / (name + suffix)).touch()
    return directory


def test_discovery_is_independent_of_working_directory(tmp_path, monkeypatch):
    root = tmp_path / "app"
    bundled = make_pair(root / "tools" / "ffmpeg")
    monkeypatch.chdir(tmp_path)
    found = service.discover_engine(app_root=root, allow_path=False)
    assert found.ffmpeg.parent == bundled and found.source == "bundled"


def test_explicit_directory_has_priority_and_does_not_silently_fallback(tmp_path):
    make_pair(tmp_path / "tools" / "ffmpeg")
    custom = make_pair(tmp_path / "custom")
    assert service.discover_engine(custom, app_root=tmp_path).source == "configured"
    with pytest.raises(service.FFmpegError, match="same directory"):
        service.discover_engine(tmp_path / "missing", app_root=tmp_path)


def test_path_lookup_requires_probe_beside_ffmpeg(tmp_path, monkeypatch):
    executable = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    executable.touch()
    monkeypatch.setattr(service.shutil, "which", lambda _: str(executable))
    with pytest.raises(service.FFmpegError) as exc:
        service.discover_engine(app_root=tmp_path, allow_path=True)
    assert exc.value.code == "engine_missing"


def test_frozen_application_does_not_fallback_to_path(tmp_path, monkeypatch):
    make_pair(tmp_path / "path")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(service.shutil, "which", lambda _: pytest.fail("PATH must not be searched"))
    with pytest.raises(service.FFmpegError) as exc:
        service.discover_engine(app_root=tmp_path)
    assert exc.value.code == "engine_missing"


def test_portable_engine_is_beside_executable_not_internal_or_cwd(tmp_path, monkeypatch):
    package = tmp_path / "portable app"
    expected = make_pair(package / "tools" / "ffmpeg")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(package / "ImadeoCovert.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(package / "_internal"), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service.shutil, "which", lambda _: pytest.fail("Portable app must not use PATH"))
    assert service.discover_engine().ffmpeg.parent == expected


def test_capabilities_distinguish_container_and_encoder_support():
    paths = service.EnginePaths(Path("ffmpeg"), Path("ffprobe"), "test")
    capabilities = service.EngineCapabilities(paths, "test", "", frozenset({"aac", "libopus"}),
                                               frozenset({"mp4", "matroska", "webm"}))
    assert capabilities.missing_for(VIDEO_PRESETS[0]) == ("encoder:libx264",)
    assert capabilities.missing_for(VIDEO_PRESETS[2]) == ("encoder:libvpx-vp9",)


def test_rejects_different_builds(monkeypatch):
    paths = service.EnginePaths(Path("ffmpeg"), Path("ffprobe"), "test")
    monkeypatch.setattr(service, "_run", lambda args, **_: f"{args[0]} version 9.0\nconfiguration: --{args[0]}\n")
    with pytest.raises(service.FFmpegError) as exc:
        service.inspect_engine(paths)
    assert exc.value.code == "engine_mismatch"


def test_process_drains_both_streams_without_deadlocking():
    script = "import sys; sys.stderr.write('e'*200000); sys.stdout.write('ok'*100000)"
    assert service._run([sys.executable, "-c", script], timeout=10) == "ok" * 100000


def test_capability_legends_are_not_detected_as_codecs(monkeypatch):
    paths = service.EnginePaths(Path("ffmpeg"), Path("ffprobe"), "test")

    def fake_run(args, **kwargs):
        if "-version" in args:
            return f"{args[0]} version 9.0\nconfiguration: --test\n"
        return " V..... = Video\n A..... = Audio\n E = Muxing supported\n"

    monkeypatch.setattr(service, "_run", fake_run)
    with pytest.raises(service.FFmpegError) as exc:
        service.inspect_engine(paths)
    assert exc.value.code == "invalid_engine"


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_reap_the_process(monkeypatch, cancel):
    started = []
    real_popen = subprocess.Popen

    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(service.subprocess, "Popen", record)
    event = threading.Event()
    timer = threading.Timer(0.3, event.set) if cancel else None
    if timer:
        timer.start()
    try:
        with pytest.raises(service.FFmpegError) as exc:
            service._run([sys.executable, "-c", "import time; time.sleep(30)"],
                         timeout=5 if cancel else 0.3, cancel_event=event)
        assert exc.value.code == ("cancelled" if cancel else "timeout")
        assert len(started) == 1 and started[0].poll() is not None
    finally:
        if timer:
            timer.cancel()
            timer.join()


def test_pre_cancel_does_not_launch_a_process(monkeypatch):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: pytest.fail("Must not launch"))
    with pytest.raises(service.FFmpegError) as exc:
        service._run(["unused"], timeout=1, cancel_event=event)
    assert exc.value.code == "cancelled"


def test_process_errors_keep_bounded_diagnostics():
    with pytest.raises(service.FFmpegError) as exc:
        service._run([sys.executable, "-c", "import sys; sys.stderr.write('e'*20000); sys.exit(3)"], timeout=5)
    assert exc.value.code == "process_failed" and len(exc.value.details) == 8192


def test_installer_rejects_archive_before_installing(tmp_path):
    from scripts.setup_ffmpeg import install
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"bad download")
    destination = tmp_path / "tools"
    with pytest.raises(ValueError, match="SHA-256"):
        install(archive, {"archive_sha256": "0" * 64}, destination)
    assert not destination.exists()
