"""UI-owned background operations. Workers communicate exclusively through queues.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import queue
import threading
from uuid import uuid4

from ffmpeg_service import FFmpegError, discover_engine, inspect_engine, probe_media
from task_runner import TaskRunner


class VideoSession:
    def __init__(self):
        self.events = queue.Queue()
        self.thread = None
        self.stop = threading.Event()
        self.operation_id = None

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def cancel(self):
        self.stop.set()

    def _start(self, work):
        if self.busy:
            raise RuntimeError("A video operation is still running")
        self.operation_id = uuid4().hex
        operation_id = self.operation_id
        self.stop = threading.Event()
        stop = self.stop

        def emit(kind, value):
            self.events.put((operation_id, kind, value))

        def run():
            try:
                work(stop, emit)
            except Exception as exc:
                emit("error", exc)
            finally:
                emit("done", None)

        self.thread = threading.Thread(target=run, name="imadeo-video", daemon=False)
        self.thread.start()

    def inspect(self, directory):
        self._start(lambda stop, emit: emit("engine", inspect_engine(
            discover_engine(directory or None), cancel_event=stop)))

    def probe(self, paths, engine):
        snapshot = tuple(paths)

        def work(stop, emit):
            for row_id, path in snapshot:
                if stop.is_set():
                    break
                try:
                    emit("media", (row_id, probe_media(path, engine.paths, cancel_event=stop)))
                except FFmpegError as exc:
                    if exc.code != "cancelled":
                        emit("media_error", (row_id, exc))
        self._start(work)

    def convert(self, tasks, engine):
        self._start(lambda stop, emit: TaskRunner(engine).run(
            tasks, cancel_event=stop, events=self.events))
