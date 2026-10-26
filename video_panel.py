"""Video workspace. All widget access happens on the Tk owner thread.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from dataclasses import dataclass, replace
from pathlib import Path
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from uuid import uuid4

from app_metadata import APP_NAME
from converter import format_size
from file_utils import path_key
from media_models import ConversionResult, TaskEvent, VideoOptions, VIDEO_PRESETS
from task_runner import make_tasks
from video_session import VideoSession
from video_strings import translate

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts", ".mts", ".m2ts"}


@dataclass
class VideoRow:
    path: Path
    media: object = None
    error: object = None
    result: object = None
    state: str = "queued"
    destination: Path | None = None


def duration_text(seconds):
    if seconds is None:
        return "—"
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


class VideoPanel(tk.Frame):
    def __init__(self, parent, app, palette, open_path):
        super().__init__(parent, bg=palette["BG"], padx=16, pady=12)
        self.app, self.palette, self.open_path = app, palette, open_path
        self.session = VideoSession()
        self.engine = None
        self.engine_error = None
        self.rows = {}
        self.batch_id = None
        self.active_tasks = set()
        self.operation = None
        self.pending_clear = False
        self._done_received = False
        self.closed = False
        self.labels = []
        self.controls = []
        saved = app._settings.get("video", {}) if app._remember_settings.get() else {}
        self.values = {"preset": saved.get("preset", "mp4"), "quality": saved.get("quality", "balanced"),
                       "speed": saved.get("speed", "balanced"), "max_height": saved.get("max_height")}
        self.out_dir = tk.StringVar(self, saved.get("out_dir", ""))
        self.mute = tk.BooleanVar(self, saved.get("mute", False))
        self.engine_dir = app._settings.get("engine_dir", "")
        self._build()
        self.retranslate()
        self.reload_engine()
        self._poll_id = self.app.after(75, self._poll)

    def t(self, key):
        return translate(self.app._lang, key)

    @property
    def busy(self):
        # The operation is released only after both the done event and thread exit.
        return self.operation is not None

    def _label(self, parent, key, **kwargs):
        widget = tk.Label(parent, bg=self.palette["BG"], fg=self.palette["FG2"],
                          font=("Segoe UI", 10), anchor="w", **kwargs)
        self.labels.append((widget, key))
        return widget

    def _button(self, parent, key, command):
        button = ttk.Button(parent, command=command)
        self.labels.append((button, key))
        button.pack(side="left", padx=(0, 7))
        return button

    def _build(self):
        engine_bar = tk.Frame(self, bg=self.palette["BG"])
        engine_bar.pack(fill="x")
        self.engine_label = self._label(engine_bar, "engine_checking")
        self.engine_label.pack(side="left", fill="x", expand=True)
        self.engine_details_btn = self._button(engine_bar, "details", self.show_engine_details)
        tools = engine_bar
        self.engine_buttons = [self._button(tools, key, command) for key, command in (
            ("engine_choose", self.choose_engine), ("engine_auto", self.default_engine), ("reload", self.reload_engine))]
        toolbar = tk.Frame(self, bg=self.palette["BG"])
        toolbar.pack(fill="x", pady=(8, 8))
        self.add_btn = self._button(toolbar, "add", self.add_dialog)
        self.clear_btn = self._button(toolbar, "clear", self.clear)
        self.retry_btn = self._button(toolbar, "retry", lambda: self.start(retry=True))
        self._button(toolbar, "play", self.play)
        self._button(toolbar, "details", self.show_details)
        self.count_label = self._label(toolbar, "count")
        self.count_label.pack(side="right")

        tables = tk.Frame(self, bg=self.palette["BG"])
        tables.pack(fill="both", expand=True)
        tables.columnconfigure((0, 1), weight=1, uniform="video")
        tables.rowconfigure(0, weight=1)
        self.source_tree = self._tree(tables, 0, "sources", ("name", "duration", "media", "size"), (170, 70, 190, 65))
        self.result_tree = self._tree(tables, 1, "results", ("state", "name", "resolution", "size"), (85, 180, 95, 65))
        self.source_tree.bind("<<TreeviewSelect>>", lambda e: self._selection(self.source_tree, self.result_tree))
        self.result_tree.bind("<<TreeviewSelect>>", lambda e: self._selection(self.result_tree, self.source_tree))
        self.source_tree.bind("<Double-1>", lambda e: self.show_details() if self.source_tree.identify_row(e.y) else None)
        self.result_tree.bind("<Double-1>", lambda e: self.play() if self.result_tree.identify_row(e.y) else None)
        self.selection_label = self._label(self, "select_hint", height=1)
        self.selection_label.pack(fill="x", pady=(6, 4))

        options = tk.Frame(self, bg=self.palette["BG"])
        options.pack(fill="x", pady=6)
        self.choices = {
            "preset": [("mp4", "MP4"), ("mkv", "MKV"), ("webm", "WebM")],
            "quality": [("high", "high"), ("balanced", "balanced"), ("compact", "compact")],
            "speed": [("fast", "fast"), ("balanced", "balanced"), ("slow", "slow")],
            "max_height": [(None, "original"), (1080, "1080p"), (720, "720p")],
        }
        self.combos = {}
        for field, label in (("preset", "format"), ("quality", "quality"), ("speed", "speed"), ("max_height", "height")):
            block = tk.Frame(options, bg=self.palette["BG"])
            block.pack(side="left", padx=(0, 16))
            self._label(block, label).pack(anchor="w")
            combo = ttk.Combobox(block, state="readonly", width=16)
            combo.pack(pady=(3, 0))
            combo.bind("<<ComboboxSelected>>", lambda e, key=field: self._option_changed(key))
            self.combos[field] = combo
        mute = ttk.Checkbutton(options, variable=self.mute, command=self._mute_changed)
        mute.pack(side="left", pady=(18, 0))
        self.labels.append((mute, "mute"))
        self.controls.append(mute)

        folder = tk.Frame(self, bg=self.palette["BG"])
        folder.pack(fill="x", pady=(4, 6))
        self._label(folder, "folder").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(folder, textvariable=self.out_dir)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.bind("<FocusOut>", lambda e: self._save())
        self.controls += [entry, self._button(folder, "choose", self.choose_output)]
        self._button(folder, "open_folder", self.open_folder)
        for key in ("hint", "tracks"):
            self._label(self, key).pack(fill="x")

        footer = tk.Frame(self, bg=self.palette["BG"])
        footer.pack(fill="x", pady=(8, 0))
        self.convert_btn = self._button(footer, "convert", self.start)
        self.cancel_btn = self._button(footer, "cancel", self.cancel)
        self.progress = ttk.Progressbar(footer, maximum=100, style="P.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.batch_label = self._label(self, "ready")
        self.batch_label.pack(fill="x", pady=(5, 0))
        self.current_label = self._label(self, "ready")
        self.current_label.pack(fill="x")

    def _tree(self, parent, column, title, columns, widths):
        frame = tk.Frame(parent, bg=self.palette["BG"])
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
        self._label(frame, title).pack(fill="x", pady=(0, 4))
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=2)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill="both", expand=True)
        for key, width in zip(columns, widths):
            tree.column(key, width=width, minwidth=45, stretch=True)
        tree.tag_configure("failed", foreground=self.palette["ACCENT2"])
        tree.tag_configure("succeeded", foreground=self.palette["GREEN"])
        return tree

    def register_drop(self, drop_type):
        for widget in (self, self.source_tree, self.result_tree):
            widget.drop_target_register(drop_type)
            widget.dnd_bind("<<Drop>>", lambda e: self.add_paths(self.tk.splitlist(e.data)))

    def preferences(self):
        return {**self.values, "mute": self.mute.get(), "out_dir": self.out_dir.get()}

    def _save(self):
        self.app._save_settings()

    def _option_changed(self, key):
        index = self.combos[key].current()
        if index >= 0:
            self.values[key] = self.choices[key][index][0]
        self._save()

    def _mute_changed(self):
        self._refresh_presets()
        self._save()

    def _refresh_presets(self):
        if self.engine:
            self.choices["preset"] = [(preset.id, preset.id.upper()) for preset in VIDEO_PRESETS
                                      if preset.video_encoder in self.engine.encoders and preset.muxer in self.engine.muxers
                                      and (self.mute.get() or preset.audio_encoder in self.engine.encoders)]
        self._render_combos()
        self._update_controls()

    def _render_combos(self):
        for field, combo in self.combos.items():
            choices = self.choices[field]
            keys = [value for value, label in choices]
            if self.values[field] not in keys:
                self.values[field] = keys[0] if keys else None
            combo.configure(values=[self.t(label) for value, label in choices])
            if keys:
                combo.current(keys.index(self.values[field]))
            else:
                combo.set("—")

    def retranslate(self):
        for widget, key in self.labels:
            widget.configure(text=self.t(key))
        for tree in (self.source_tree, self.result_tree):
            for key in tree["columns"]:
                tree.heading(key, text=self.t(key))
        self._render_combos()
        self._render_engine()
        for row_id in self.rows:
            self._render_row(row_id)
        self._update_counts()
        self._update_controls()

    def _render_engine(self):
        if self.operation == "engine":
            text = self.t("engine_checking")
        elif self.engine:
            text = self.t("engine_ready").format(version=self.engine.version.split()[0], source=self.engine.paths.source)
        else:
            text = self.t("engine_missing" if getattr(self.engine_error, "code", "") == "engine_missing" else "engine_problem")
        self.engine_label.configure(text=text)

    def _update_controls(self):
        idle = not self.busy and not self.closed
        for button in [self.add_btn, *self.engine_buttons, *self.controls]:
            button.configure(state="normal" if idle else "disabled")
        for combo in self.combos.values():
            combo.configure(state="readonly" if idle else "disabled")
        can_start = idle and self.engine is not None and bool(self.choices["preset"]) and bool(self.rows)
        self.convert_btn.configure(state="normal" if can_start else "disabled")
        self.retry_btn.configure(state="normal" if can_start and any(r.state in ("failed", "cancelled") for r in self.rows.values()) else "disabled")
        self.cancel_btn.configure(state="normal" if self.busy and not self.session.stop.is_set() else "disabled")
        self.clear_btn.configure(state="disabled" if self.pending_clear or self.closed else "normal")

    def _begin(self, operation):
        self.operation = operation
        self._done_received = False
        self._update_controls()

    def reload_engine(self):
        if self.busy or self.closed:
            return
        self.engine, self.engine_error = None, None
        self._begin("engine")
        self.session.inspect(self.engine_dir)
        self._render_engine()

    def choose_engine(self):
        directory = filedialog.askdirectory(parent=self, title=self.t("engine_choose"))
        if directory and not self.busy:
            self.engine_dir = directory
            self._save()
            self.reload_engine()

    def default_engine(self):
        if not self.busy:
            self.engine_dir = ""
            self._save()
            self.reload_engine()

    def add_dialog(self):
        self.add_paths(filedialog.askopenfilenames(parent=self, title=self.t("add"), filetypes=[
            (self.t("video_mode"), " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS)))]))

    def add_paths(self, paths):
        if self.busy or self.closed:
            return "break"
        known = {path_key(row.path) for row in self.rows.values()}
        skipped = 0
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            key = path_key(path)
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS or key in known:
                skipped += 1
                continue
            known.add(key)
            row_id = uuid4().hex
            self.rows[row_id] = VideoRow(path)
            self.source_tree.insert("", "end", iid=row_id)
            self.result_tree.insert("", "end", iid=row_id)
            self._render_row(row_id)
        self.current_label.configure(text=self.t("skipped").format(count=skipped) if skipped else self.t("ready"))
        self._update_counts()
        if self.rows and not self.source_tree.selection():
            self.source_tree.selection_set(next(iter(self.rows)))
        self._probe_pending()
        self._update_controls()
        return "break"

    def _probe_pending(self):
        pending = [(key, row.path) for key, row in self.rows.items() if row.media is None and row.error is None]
        if pending and self.engine and not self.busy and not self.closed:
            self._begin("probe")
            self.session.probe(pending, self.engine)

    def _media_text(self, media):
        if not media:
            return "—"
        video = media.primary_video
        width, height = video.display_size
        fps = video.average_frame_rate or video.nominal_frame_rate
        return f"{width}×{height} · {float(fps):.2f} fps · {video.codec_name}" if fps else f"{width}×{height} · {video.codec_name}"

    def _render_row(self, key):
        row = self.rows[key]
        media = row.media
        try:
            size = format_size(row.path.stat().st_size)
        except OSError:
            size = "—"
        self.source_tree.item(key, values=(row.path.name, duration_text(media.duration if media else None),
                                          self._media_text(media), size))
        result_media = row.result.media if row.result else None
        dimensions = "×".join(map(str, result_media.primary_video.display_size)) if result_media else "—"
        self.result_tree.item(key, values=(self.t(row.state), row.destination.name if row.destination else "—",
                                          dimensions, format_size(result_media.size_bytes) if result_media else "—"), tags=(row.state,))
        if self.source_tree.selection() == (key,):
            self.selection_label.configure(text=self._summary(row))

    def _selection(self, selected, other):
        selection = selected.selection()
        if not selection:
            return
        key = selection[0]
        if other.selection() != selection:
            other.selection_set(key)
        row = self.rows.get(key)
        if row:
            self.selection_label.configure(text=self._summary(row))

    def _summary(self, row):
        if row.error:
            return self.t("probe_error")
        if row.result and row.result.error_code:
            return self.t(row.result.error_code) if row.result.error_code in ("cancelled", "encode_failed", "unsupported_color", "invalid_output", "file_error", "timeout", "source_conflict", "output_exists", "input_missing", "no_video", "unsupported_preset") else self.t("unknown_error")
        media = row.media
        if not media:
            return self.t("probing")
        text = f"{row.path.name} · {self._media_text(media)} · "
        text += self.t("audio").format(count=len(media.audio_streams)) if media.audio_streams else self.t("no_audio")
        video = media.primary_video
        if video.is_hdr or video.has_alpha:
            text += " · " + self.t("color_warning")
        elif video.bits_per_raw_sample and video.bits_per_raw_sample > 8:
            text += " · " + self.t("depth_warning")
        if len(media.audio_streams) > 1 or len(media.video_streams) > 1 or media.subtitle_streams:
            text += " · " + self.t("track_warning")
        return text

    def choose_output(self):
        path = filedialog.askdirectory(parent=self, title=self.t("folder"))
        if path:
            self.out_dir.set(path)
            self._save()

    def start(self, retry=False):
        if self.busy or self.closed or not self.engine or self.app._running or self.app._mode != "video":
            return
        selected = [(key, row) for key, row in self.rows.items() if not retry or row.state in ("failed", "cancelled")]
        if not selected:
            return
        if not self.out_dir.get().strip():
            self.choose_output()
        if not self.out_dir.get().strip():
            return
        options = VideoOptions(**self.values, mute=self.mute.get())
        try:
            tasks = make_tasks([row.path for key, row in selected], Path(self.out_dir.get()), options)
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        tasks = tuple(replace(task, task_id=key) for task, (key, row) in zip(tasks, selected))
        self.batch_id = tasks[0].batch_id
        self.active_tasks = {task.task_id for task in tasks}
        for task in tasks:
            row = self.rows[task.task_id]
            row.state, row.result, row.error, row.destination = "queued", None, None, task.destination
            self._render_row(task.task_id)
        self.progress["value"] = 0
        self._save()
        self._begin("convert")
        self.app._set_batch_busy(True)
        self.session.convert(tasks, self.engine)
        self._update_counts()

    def cancel(self):
        if self.busy:
            self.session.cancel()
            self.current_label.configure(text=self.t("cancelling"))
            self._update_controls()

    def clear(self):
        if self.busy:
            self.pending_clear = True
            self.cancel()
            return
        self.batch_id = None
        self.active_tasks.clear()
        self.rows.clear()
        for tree in (self.source_tree, self.result_tree):
            tree.delete(*tree.get_children())
        self.progress["value"] = 0
        self.selection_label.configure(text=self.t("select_hint"))
        self.current_label.configure(text=self.t("ready"))
        self._update_counts()
        self._update_controls()

    def _update_counts(self):
        self.count_label.configure(text=self.t("count").format(count=len(self.rows)))
        states = [self.rows[key].state for key in self.active_tasks if key in self.rows]
        if states:
            counts = {state: states.count(state) for state in ("succeeded", "failed", "cancelled")}
            self.batch_label.configure(text=self.t("batch").format(done=sum(counts.values()), total=len(states),
                                       ok=counts["succeeded"], failed=counts["failed"], cancelled=counts["cancelled"]))
        else:
            self.batch_label.configure(text="")

    def _task_event(self, event):
        if event.batch_id != self.batch_id or event.task_id not in self.active_tasks:
            return
        row = self.rows.get(event.task_id)
        if row is None or row.result is not None:
            return
        row.state = event.state
        if event.result:
            row.result = event.result
        self._render_row(event.task_id)
        if event.progress:
            progress = event.progress
            self.progress["value"] = progress.percent or 0
            percent = f"{progress.percent:.1f}%" if progress.percent is not None else self.t("elapsed").format(seconds=round(progress.processed_seconds, 1))
            self.current_label.configure(text=self.t("current").format(name=row.path.name[:55], percent=percent,
                speed=f"{progress.speed:.2f}×" if progress.speed else "—", eta=duration_text(progress.remaining_seconds)))
        else:
            if event.state in ("queued", "probing"):
                self.progress["value"] = 0
            self.current_label.configure(text=f"{row.path.name[:55]} · {self.t(event.state)}")
        if event.result and event.result.error_code:
            self.selection_label.configure(text=f"{row.path.name}: {self._summary(row)}")
        self._update_counts()

    def _poll(self):
        if self.closed:
            return
        # Bound each GUI tick even when a producer emits a burst of events.
        for _ in range(100):
            try:
                event = self.session.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, TaskEvent):
                self._task_event(event)
                continue
            operation_id, kind, payload = event
            if operation_id != self.session.operation_id:
                continue
            if kind == "engine":
                self.engine = payload
                for row in self.rows.values():
                    row.media, row.error = None, None
                self._refresh_presets()
            elif kind in ("media", "media_error"):
                key, value = payload
                if key in self.rows:
                    if kind == "media":
                        self.rows[key].media = value
                    else:
                        self.rows[key].error = value
                    self._render_row(key)
            elif kind == "error":
                if self.operation == "engine":
                    self.engine_error = payload
                else:
                    self.current_label.configure(text=f"{self.t('unknown_error')} {payload}")
                    if self.operation == "convert":
                        for key in self.active_tasks:
                            row = self.rows[key]
                            if row.result is None:
                                result = ConversionResult(key, self.batch_id, row.path, row.destination, "failed",
                                                          error_code="internal_error", error_message=str(payload))
                                self._task_event(TaskEvent(self.batch_id, key, "failed", result=result))
            elif kind == "done":
                self._done_received = True
        if self._done_received and not self.session.busy:
            previous = self.operation
            self.operation = None
            self._done_received = False
            if previous == "convert":
                self.app._set_batch_busy(False)
            self._render_engine()
            if self.pending_clear:
                self.pending_clear = False
                self.clear()
            elif previous == "engine":
                self._probe_pending()
            self._update_controls()
        self._poll_id = self.app.after(75, self._poll)

    def play(self):
        selection = self.result_tree.selection()
        if selection:
            row = self.rows.get(selection[0])
            if row and row.result and row.result.state == "succeeded" and row.destination.is_file():
                self.open_path(str(row.destination))

    def open_folder(self):
        if self.out_dir.get() and Path(self.out_dir.get()).is_dir():
            self.open_path(self.out_dir.get())

    def _details_window(self, title, text):
        win = tk.Toplevel(self)
        win.title(f"{APP_NAME} · {title}")
        win.geometry("820x480")
        from tkinter.scrolledtext import ScrolledText
        box = ScrolledText(win, wrap="word", font=("Consolas", 10), bg=self.palette["BG2"], fg=self.palette["FG"])
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")

    def show_engine_details(self):
        text = self.t("engine_checking")
        if self.engine:
            missing = "\n".join(f"{preset.id}: {', '.join(self.engine.missing_for(preset)) or 'OK'}" for preset in VIDEO_PRESETS)
            text = f"{self.engine.paths.ffmpeg}\n{self.engine.paths.ffprobe}\n{self.engine.version}\n{missing}\n\n{self.engine.configuration}"
        elif self.engine_error:
            text = f"{self.engine_error}\n{getattr(self.engine_error, 'details', '')}"
        self._details_window(self.t("engine_details"), text)

    def show_details(self):
        selection = self.source_tree.selection() or self.result_tree.selection()
        if not selection:
            return
        row = self.rows[selection[0]]
        text = f"{row.path}\n\n{self._summary(row)}\n\n{self.t('tracks')}\n{self.t('safe_names')}"
        if row.error:
            text += f"\n\n{row.error}\n{getattr(row.error, 'details', '')}"
        if row.result:
            result = row.result
            text += f"\n\n{result.destination}\n{result.state}\n{result.error_code or ''}\n{result.error_message or ''}\n"
            text += "\n".join(result.warnings) + "\n\n" + result.details
        self._details_window(row.path.name, text)

    def shutdown(self):
        self.closed = True
        self.session.cancel()
        self.app.after_cancel(self._poll_id)
        self._update_controls()
