"""Run native Tk/DnD cases in separate processes, as the desktop app runs.

Repeatedly loading/unloading Tcl and native tkdnd in one Windows process can
leave interpreter initialization state behind. Isolation also catches leaked
non-daemon workers: the child must exit within the timeout after each case.
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("case", [
    pytest.param("test_missing_engine_disables_only_video_and_can_recover", marks=pytest.mark.integration),
    pytest.param("test_real_gui_batch_retry_and_mode_isolation", marks=pytest.mark.integration),
    pytest.param("test_cancel_clear_waits_and_restart_uses_fresh_identity", marks=pytest.mark.integration),
    pytest.param("test_close_reaps_active_video_processes", marks=pytest.mark.integration),
    "test_image_conversion_still_works_and_locks_mode",
    "test_minimum_window_layout_and_language_values[light]",
    "test_minimum_window_layout_and_language_values[dark]",
    "test_image_clear_waits_for_existing_worker_before_mode_unlock",
    pytest.param("test_capability_filter_respects_mute_and_can_recover_options", marks=pytest.mark.integration),
    "test_update_identity_and_development_version",
])
def test_gui_in_own_process(case):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{Path(__file__).with_name('gui_cases.py')}::{case}",
         "-p", "no:cacheprovider"], capture_output=True, encoding="utf-8", env=env,
        timeout=60, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if "1 skipped" in result.stdout:
        pytest.skip("The child GUI case requires an installed FFmpeg toolchain")
