"""Serial video batches with per-run cancellation and immutable task identities.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from pathlib import Path
import queue
import threading
from uuid import uuid4

from ffmpeg_service import EngineCapabilities, FFmpegError
from file_utils import allocate_output, path_key
from media_models import ConversionResult, MediaTask, TaskEvent, VideoOptions
from video_converter import convert_video, validate_options


def make_tasks(sources: list[Path], output_dir: Path, options: VideoOptions) -> tuple[MediaTask, ...]:
    preset = validate_options(options)
    snapshot = tuple(path.expanduser().resolve() for path in sources)
    batch_id = uuid4().hex
    reserved: set[str] = set()
    return tuple(MediaTask(source, allocate_output(output_dir, source.stem, preset.extension,
                                                  reserved=reserved, sources=snapshot,
                                                  overwrite=options.overwrite), options, batch_id)
                 for source in snapshot)


class TaskRunner:
    """Blocking worker API. Run on a UI-owned thread and consume events on the UI thread.

    The caller owns a new Event for each run. Cancellation stops the current process
    and marks unstarted tasks cancelled; no event object is reused or cleared here.
    """

    def __init__(self, engine: EngineCapabilities):
        self.engine = engine
        self._lock = threading.Lock()

    def run(self, tasks: tuple[MediaTask, ...], *, cancel_event: threading.Event | None = None,
            events: queue.Queue | None = None) -> tuple[ConversionResult, ...]:
        if not self._lock.acquire(blocking=False):
            raise FFmpegError("batch_running", "This runner already has an active batch.")
        try:
            snapshot = tuple(tasks)
            if not snapshot:
                return ()
            if (len({t.batch_id for t in snapshot}) != 1 or
                    len({t.task_id for t in snapshot}) != len(snapshot) or
                    len({path_key(t.destination) for t in snapshot}) != len(snapshot)):
                raise FFmpegError("invalid_batch", "A batch requires unique task IDs and output paths.")
            stop = cancel_event if cancel_event is not None else threading.Event()
            sources = tuple(t.source for t in snapshot)
            for task in snapshot:
                if events is not None:
                    events.put(TaskEvent(task.batch_id, task.task_id, "queued"))
            return tuple(convert_video(task, self.engine, cancel_event=stop,
                                       events=events, protected_sources=sources) for task in snapshot)
        finally:
            self._lock.release()
