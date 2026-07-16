"""Tkinter desktop interface for MSG to Markdown."""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

from config import APP_DIR, AppConfig
from converter import (
    ConversionCallbacks,
    ConversionOptions,
    ConversionResult,
    ConversionStats,
    ConversionStatus,
    MsgToMarkdownConverter,
)
from logger import add_queue_handler
from notifications import DesktopNotifier
from startup import (
    StartupRegistrationError,
    disable_watcher_startup,
    enable_watcher_startup,
    is_watcher_startup_enabled,
)
from watcher import FolderWatcher, WatcherCallbacks


UiEvent = tuple[str, Any]
THEME_OPTIONS = ("system", "light", "dark")

THEME_PALETTES = {
    "light": {
        "window": "#f6f7f9",
        "panel": "#ffffff",
        "panel_alt": "#f0f2f5",
        "input": "#ffffff",
        "button": "#f3f4f6",
        "button_hover": "#e8edf5",
        "button_pressed": "#dce6f7",
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "primary_text": "#ffffff",
        "text": "#111827",
        "secondary_text": "#4b5563",
        "disabled_text": "#8a94a6",
        "border": "#d5dae3",
        "focus": "#2563eb",
        "progress": "#2563eb",
        "log_bg": "#ffffff",
        "log_info": "#1f2937",
        "log_warning": "#8a4b00",
        "log_error": "#b42318",
        "selection_bg": "#bfdbfe",
        "selection_text": "#111827",
        "title_bar": "#f6f7f9",
        "title_text": "#111827",
        "indicator": "#ffffff",
    },
    "dark": {
        "window": "#191919",
        "panel": "#202020",
        "panel_alt": "#2b2b2b",
        "input": "#1f1f1f",
        "button": "#2b2b2b",
        "button_hover": "#343434",
        "button_pressed": "#3c3c3c",
        "primary": "#60cdff",
        "primary_hover": "#8bdcff",
        "primary_text": "#111111",
        "text": "#f3f3f3",
        "secondary_text": "#c9c9c9",
        "disabled_text": "#858585",
        "border": "#3f3f3f",
        "focus": "#60cdff",
        "progress": "#60cdff",
        "log_bg": "#111111",
        "log_info": "#f3f3f3",
        "log_warning": "#ffd166",
        "log_error": "#ff8a80",
        "selection_bg": "#0067c0",
        "selection_text": "#ffffff",
        "title_bar": "#202020",
        "title_text": "#f3f3f3",
        "indicator": "#2b2b2b",
    },
}


class ToolTip:
    """Small tooltip for long path fields."""

    def __init__(self, widget: tk.Widget, text_var: tk.StringVar) -> None:
        self.widget = widget
        self.text_var = text_var
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        widget.bind("<FocusOut>", self.hide)

    def show(self, _event: tk.Event[Any] | None = None) -> None:
        text = self.text_var.get()
        if not text or self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip,
            text=text,
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=4,
        )
        label.pack()

    def hide(self, _event: tk.Event[Any] | None = None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class MsgToMarkdownApp:
    """Main Tkinter application controller."""

    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        logger: logging.Logger,
        notifier: DesktopNotifier,
        start_minimized: bool = False,
        start_watching: bool = False,
    ) -> None:
        self.root = root
        self.config = config
        self.logger = logger
        self.notifier = notifier
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue[UiEvent]" = queue.Queue()
        add_queue_handler(self.logger, self.log_queue)

        self.converter = MsgToMarkdownConverter(logger=self.logger)
        self.watcher: FolderWatcher | None = None
        self.worker_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.batch_running = False
        self.exiting = False
        self.tray_icon: object | None = None
        self.tray_thread: threading.Thread | None = None
        self.start_minimized = start_minimized
        self.start_watching_override = start_watching
        self.watch_input_folder = config.input_folder
        self.active_watch_input_folder: Path | None = None
        self.active_watch_output_folder: Path | None = None
        self.selected_input_files: list[Path] = []
        self.selected_input_display = ""
        self.tk_theme_widgets: list[tk.Widget] = []
        self.panel_widgets: list[tk.Widget] = []
        self.tooltip_widgets: list[tk.Toplevel] = []
        self.style = ttk.Style()
        self._use_customisable_ttk_theme()
        self.window_icons = self._load_window_icons()

        self.input_var = tk.StringVar(value=config.input_folder)
        self.output_var = tk.StringVar(value=config.output_folder)
        self.mode_var = tk.StringVar(value=config.app_mode)
        self.process_subfolders_var = tk.BooleanVar(value=config.process_subfolders)
        self.open_output_var = tk.BooleanVar(value=config.open_output_when_finished)
        self.auto_watch_var = tk.BooleanVar(value=config.auto_start_watching)
        self.minimize_tray_var = tk.BooleanVar(value=config.minimize_to_tray)
        self.theme_var = tk.StringVar(value=self._normalise_theme(config.theme))
        self.login_startup_var = tk.BooleanVar(
            value=self._load_login_startup_state()
        )
        self.notifications_var = tk.BooleanVar(
            value=config.enable_desktop_notifications
        )
        self.notifier.enabled = self.notifications_var.get()
        self.preferences_expanded = config.details_expanded

        self.status_var = tk.StringVar(value="Watcher stopped")
        self.current_file_var = tk.StringVar(value="No file selected")
        self.progress_var = tk.DoubleVar(value=0)
        self.processed_var = tk.StringVar(value="0")
        self.succeeded_var = tk.StringVar(value="0")
        self.failed_var = tk.StringVar(value="0")
        self.skipped_var = tk.StringVar(value="0")
        self._stats = {
            ConversionStatus.SUCCEEDED: 0,
            ConversionStatus.FAILED: 0,
            ConversionStatus.SKIPPED: 0,
            "processed": 0,
        }

        self._build_window()
        self._apply_theme()
        self._refresh_mode_ui()
        self._refresh_settings_dependencies()
        self._set_preferences_expanded(self.preferences_expanded, resize=True)
        self._schedule_queue_drain()
        if self.start_minimized:
            self.root.after(250, self._hide_to_tray)
        if self.auto_watch_var.get() or self.start_watching_override:
            self.mode_var.set("watch")
            self._refresh_mode_ui()
            self.root.after(600, self.start_watching)

    def _build_window(self) -> None:
        self.root.title("MSG to Markdown")
        self.root.geometry(self.config.window_geometry)
        self.root.minsize(900, 420)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self._on_unmap)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.main_frame = ttk.Frame(self.root, padding=16)
        self.main_frame.grid(row=0, column=0, sticky="nsew")
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.rowconfigure(6, weight=1)

        self._build_mode_frame(self.main_frame)
        self._build_folder_frame(self.main_frame)
        self._build_options_frame(self.main_frame)
        self._build_actions_frame(self.main_frame)
        self._build_progress_frame(self.main_frame)
        self._build_preferences_toggle(self.main_frame)
        self._build_preferences_frame(self.main_frame)

        self.status_label = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            padding=(12, 4),
            style="Status.TLabel",
        )
        self.status_label.grid(row=1, column=0, sticky="ew")

    def _build_mode_frame(self, parent: ttk.Frame) -> None:
        frame = self._panel(parent, row=0)
        frame.columnconfigure(3, weight=1)
        ttk.Label(frame, text="Mode", style="PanelHeading.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 16),
        )
        self._make_radiobutton(
            frame,
            "Convert Once",
            self.mode_var,
            "convert",
            self._on_mode_change,
        ).grid(row=0, column=1, sticky="w", padx=(0, 16))
        self._make_radiobutton(
            frame,
            "Watch Folder",
            self.mode_var,
            "watch",
            self._on_mode_change,
        ).grid(row=0, column=2, sticky="w")

    def _build_folder_frame(self, parent: ttk.Frame) -> None:
        frame = self._section(parent, "Paths", row=1)
        frame.columnconfigure(1, weight=1)

        (
            self.input_entry,
            self.input_label,
            self.input_browse_button,
        ) = self._folder_row(
            frame,
            row=0,
            label="Input Folder",
            text_var=self.input_var,
            browse_command=self.browse_input,
        )
        self._enable_input_drop_target()
        self.output_entry, _output_label, _output_browse_button = self._folder_row(
            frame,
            row=1,
            label="Output Folder",
            text_var=self.output_var,
            browse_command=self.browse_output,
        )
        for entry in (self.input_entry, self.output_entry):
            entry.bind("<FocusOut>", self._on_watch_path_entry_change)
            entry.bind("<Return>", self._on_watch_path_entry_change)

    def _folder_row(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        label: str,
        text_var: tk.StringVar,
        browse_command: Any,
    ) -> tuple[ttk.Entry, ttk.Label, ttk.Button]:
        label_widget = ttk.Label(parent, text=label, style="Panel.TLabel")
        label_widget.grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=(0 if row == 0 else 8, 0),
        )
        entry = ttk.Entry(parent, textvariable=text_var)
        entry.grid(
            row=row,
            column=1,
            sticky="ew",
            pady=(0 if row == 0 else 8, 0),
        )
        entry.configure(width=80)
        ToolTip(entry, text_var)
        button = ttk.Button(parent, text="Browse", command=browse_command)
        button.grid(
            row=row,
            column=2,
            sticky="ew",
            padx=(12, 0),
            pady=(0 if row == 0 else 8, 0),
        )
        return entry, label_widget, button

    def _build_options_frame(self, parent: ttk.Frame) -> None:
        frame = self._section(parent, "Options", row=2)
        for column in range(2):
            frame.columnconfigure(column, weight=1)

        self.process_subfolders_check = self._make_checkbutton(
            frame,
            "Process subfolders",
            self.process_subfolders_var,
        )
        self.process_subfolders_check.grid(row=0, column=0, sticky="w")

        self.open_output_check = self._make_checkbutton(
            frame,
            "Open output folder when finished",
            self.open_output_var,
        )
        self.open_output_check.grid(row=0, column=1, sticky="w", padx=(20, 0))

    def _build_actions_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, style="App.TFrame")
        frame.grid(row=3, column=0, sticky="ew", pady=(20, 0))
        frame.columnconfigure(3, weight=1)

        self.primary_button = ttk.Button(
            frame,
            text="Convert",
            command=self.start_conversion,
            style="Primary.TButton",
        )
        self.primary_button.grid(row=0, column=0, sticky="w")

        self.cancel_button = ttk.Button(
            frame,
            text="Cancel",
            command=self.cancel_conversion,
        )
        self.cancel_button.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.open_output_button = ttk.Button(
            frame,
            text="Open Output",
            command=self.open_output_folder,
        )
        self.open_output_button.grid(row=0, column=2, sticky="w", padx=(8, 0))

    def _build_progress_frame(self, parent: ttk.Frame) -> None:
        frame = self._section(parent, "Progress", row=4)
        for column in range(8):
            frame.columnconfigure(column, weight=1)

        items = [
            ("Files processed", self.processed_var),
            ("Successful", self.succeeded_var),
            ("Failed", self.failed_var),
            ("Skipped", self.skipped_var),
        ]
        for index, (label, variable) in enumerate(items):
            ttk.Label(frame, text=label, style="PanelMuted.TLabel").grid(
                row=0,
                column=index * 2,
                sticky="w",
            )
            ttk.Label(
                frame,
                textvariable=variable,
                style="Metric.TLabel",
            ).grid(row=0, column=index * 2 + 1, sticky="w", padx=(6, 20))

        ttk.Label(frame, text="Current file", style="PanelMuted.TLabel").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(12, 0),
        )
        ttk.Label(
            frame,
            textvariable=self.current_file_var,
            anchor="w",
            style="Panel.TLabel",
        ).grid(row=1, column=1, columnspan=7, sticky="ew", pady=(12, 0))

        self.progress_bar = ttk.Progressbar(
            frame,
            variable=self.progress_var,
            mode="determinate",
        )
        self.progress_bar.grid(
            row=2,
            column=0,
            columnspan=8,
            sticky="ew",
            pady=(12, 0),
        )

    def _build_preferences_toggle(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, style="App.TFrame")
        frame.grid(row=5, column=0, sticky="ew", pady=(16, 0))
        frame.columnconfigure(0, weight=1)

        self.preferences_button = ttk.Button(
            frame,
            text="Show Preferences",
            command=self.toggle_preferences,
        )
        self.preferences_button.grid(row=0, column=1, sticky="e")

    def _build_preferences_frame(self, parent: ttk.Frame) -> None:
        self.preferences_frame = ttk.Frame(parent, style="App.TFrame")
        self.preferences_frame.grid(row=6, column=0, sticky="nsew", pady=(20, 0))
        self.preferences_frame.columnconfigure(0, weight=1)
        self.preferences_frame.rowconfigure(0, weight=1)

        self._build_log_frame(self.preferences_frame)
        self._build_settings_frame(self.preferences_frame)

    def _build_log_frame(self, parent: ttk.Frame) -> None:
        frame = self._section(parent, "Live Log", row=0, pady=(0, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(frame, style="Panel.TFrame")
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Clear", command=self.clear_log).grid(
            row=0,
            column=1,
            sticky="e",
            padx=(0, 8),
        )
        ttk.Button(actions, text="Copy Log", command=self.copy_log).grid(
            row=0,
            column=2,
            sticky="e",
        )

        self.log_text = ScrolledText(
            frame,
            height=10,
            wrap="word",
            state="disabled",
            font=("Cascadia Mono", 9),
            borderwidth=1,
            relief="solid",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.bind("<MouseWheel>", self._on_log_scroll)
        self.log_text.bind("<Button-4>", self._on_log_scroll)
        self.log_text.bind("<Button-5>", self._on_log_scroll)

    def _build_settings_frame(self, parent: ttk.Frame) -> None:
        frame = self._section(parent, "Settings", row=1, pady=(20, 0))
        for column in range(4):
            frame.columnconfigure(column, weight=1)

        ttk.Label(frame, text="GUI theme", style="Panel.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 12),
        )
        for column, (label, value) in enumerate(
            [
                ("System (default)", "system"),
                ("Light", "light"),
                ("Dark", "dark"),
            ],
            start=1,
        ):
            self._make_radiobutton(
                frame,
                label,
                self.theme_var,
                value,
                self._on_theme_change,
            ).grid(row=0, column=column, sticky="w")

        self.auto_watch_check = self._make_checkbutton(
            frame,
            "Start watching when the application opens",
            self.auto_watch_var,
            self._on_auto_watch_change,
        )
        self.auto_watch_check.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(12, 0),
        )

        self.login_startup_check = self._make_checkbutton(
            frame,
            "Launch the application when I sign in",
            self.login_startup_var,
            self._on_login_startup_change,
        )
        self.login_startup_check.grid(
            row=1,
            column=2,
            columnspan=2,
            sticky="w",
            pady=(12, 0),
        )

        self.minimize_tray_check = self._make_checkbutton(
            frame,
            "Close to notification area",
            self.minimize_tray_var,
            self._save_config,
        )
        self.minimize_tray_check.grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(8, 0),
        )

        self.notifications_check = self._make_checkbutton(
            frame,
            "Show Windows desktop notifications",
            self.notifications_var,
            self._on_notifications_change,
        )
        self.notifications_check.grid(
            row=2,
            column=2,
            columnspan=2,
            sticky="w",
            pady=(8, 0),
        )

        self.startup_note_var = tk.StringVar()
        self.startup_note_label = ttk.Label(
            frame,
            textvariable=self.startup_note_var,
            style="PanelMuted.TLabel",
        )
        self.startup_note_label.grid(
            row=3,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(10, 0),
        )

    def _panel(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        pady: tuple[int, int] = (0, 0),
    ) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        frame.grid(row=row, column=0, sticky="ew", pady=pady)
        self.panel_widgets.append(frame)
        return frame

    def _section(
        self,
        parent: ttk.Frame,
        title: str,
        *,
        row: int,
        pady: tuple[int, int] = (20, 0),
    ) -> ttk.Frame:
        container = ttk.Frame(parent, style="App.TFrame")
        container.grid(row=row, column=0, sticky="nsew", pady=pady)
        container.columnconfigure(0, weight=1)
        if title:
            ttk.Label(container, text=title, style="Section.TLabel").grid(
                row=0,
                column=0,
                sticky="w",
                pady=(0, 8),
            )
        frame = ttk.Frame(container, style="Panel.TFrame", padding=16)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        self.panel_widgets.append(frame)
        return frame

    def _make_checkbutton(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        command: Any | None = None,
    ) -> tk.Checkbutton:
        button = tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            indicatoron=True,
            borderwidth=0,
            highlightthickness=1,
            padx=0,
            pady=2,
            anchor="w",
            takefocus=True,
        )
        self.tk_theme_widgets.append(button)
        return button

    def _make_radiobutton(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.StringVar,
        value: str,
        command: Any | None = None,
    ) -> tk.Radiobutton:
        button = tk.Radiobutton(
            parent,
            text=text,
            variable=variable,
            value=value,
            command=command,
            indicatoron=True,
            borderwidth=0,
            highlightthickness=1,
            padx=0,
            pady=2,
            anchor="w",
            takefocus=True,
        )
        self.tk_theme_widgets.append(button)
        return button

    def toggle_preferences(self) -> None:
        self._set_preferences_expanded(not self.preferences_expanded, resize=True)
        self._save_config()

    def _set_preferences_expanded(self, expanded: bool, *, resize: bool) -> None:
        self.preferences_expanded = expanded
        if expanded:
            self.preferences_frame.grid()
            self.main_frame.rowconfigure(6, weight=1)
            self.preferences_button.configure(text="Hide Preferences")
        else:
            self.preferences_frame.grid_remove()
            self.main_frame.rowconfigure(6, weight=0)
            self.preferences_button.configure(text="Show Preferences")

        if resize:
            self._resize_for_preferences()

    def _resize_for_preferences(self) -> None:
        if self.root.state() == "zoomed":
            return

        self.root.update_idletasks()
        width = max(self.root.winfo_width(), 900)
        target_height = max(self.root.winfo_reqheight(), 420)
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        left, top, right, bottom = get_work_area(self.root)
        max_height = max(420, bottom - top - 20)
        target_height = min(target_height, max_height)
        x = min(max(x, left), max(left, right - width))
        y = min(max(y, top), max(top, bottom - target_height))
        self.root.geometry(f"{width}x{target_height}+{x}+{y}")

    def _on_mode_change(self) -> None:
        self._refresh_mode_ui()
        self._save_config()

    def _refresh_mode_ui(self) -> None:
        mode = self.mode_var.get()
        watcher_running = self.watcher is not None and self.watcher.is_running

        if mode == "convert":
            self.input_label.configure(text="Select file(s)")
            self.input_browse_button.configure(text="Browse")
            if (
                self.selected_input_files
                and self.input_var.get() != self.selected_input_display
            ):
                self.input_var.set(self.selected_input_display)
            elif (
                not self.selected_input_files
                and self.input_var.get() == self.watch_input_folder
            ):
                self.input_var.set("")
            self.primary_button.configure(
                text="Convert",
                command=self.start_conversion,
                state="disabled" if self.batch_running else "normal",
            )
            if self.batch_running:
                self.cancel_button.grid()
            else:
                self.cancel_button.grid_remove()
            self.open_output_check.grid()
        else:
            self.input_label.configure(text="Input Folder")
            self.input_browse_button.configure(text="Browse")
            if (
                self.selected_input_files
                and self.input_var.get() == self.selected_input_display
            ) or not self.input_var.get().strip():
                self.input_var.set(self.watch_input_folder)
            elif (
                not self.selected_input_files
                and self.input_var.get() != self.watch_input_folder
                and Path(self.input_var.get()).suffix.lower() == ".msg"
            ):
                self.input_var.set(self.watch_input_folder)
            self.primary_button.configure(
                text="Stop Watching" if watcher_running else "Start Watching",
                command=self.stop_watching if watcher_running else self.start_watching,
                state="normal",
            )
            self.cancel_button.grid_remove()
            self.open_output_check.grid_remove()

    def _on_theme_change(self) -> None:
        self._apply_theme()
        self._save_config()

    def _on_auto_watch_change(self) -> None:
        self._refresh_settings_dependencies()
        self._save_config()

    def _on_login_startup_change(self) -> None:
        enabled = self.login_startup_var.get()
        try:
            if enabled:
                self.auto_watch_var.set(True)
                self.minimize_tray_var.set(True)
                enable_watcher_startup()
                self.logger.info("Enabled launch at Windows sign-in")
            else:
                disable_watcher_startup()
                self.logger.info("Disabled launch at Windows sign-in")
            self._refresh_settings_dependencies()
            self._save_config()
        except StartupRegistrationError as exc:
            self.login_startup_var.set(not enabled)
            self.logger.exception("Could not update Windows login startup")
            messagebox.showerror("Windows login startup", str(exc))

    def _refresh_settings_dependencies(self) -> None:
        if self.login_startup_var.get():
            self.auto_watch_var.set(True)
            self.auto_watch_check.configure(state="disabled")
            self.startup_note_var.set(
                "Sign-in launch starts the watcher using the current folders."
            )
        else:
            self.auto_watch_check.configure(state="normal")
            self.startup_note_var.set(
                "Start watching on open applies when you launch the app manually."
            )

    def _on_notifications_change(self) -> None:
        self.notifier.enabled = self.notifications_var.get()
        self._save_config()

    def _apply_theme(self) -> None:
        resolved_theme = self._resolved_theme()
        palette = THEME_PALETTES[resolved_theme]
        self.root.configure(background=palette["window"])
        self.root.option_add("*Font", "Segoe UI 10")
        self._apply_window_icon(resolved_theme)

        self.style.configure(
            ".",
            background=palette["window"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
        )
        self.style.configure("App.TFrame", background=palette["window"])
        self.style.configure("Panel.TFrame", background=palette["panel"])
        self.style.configure(
            "TLabel",
            background=palette["window"],
            foreground=palette["text"],
        )
        self.style.configure(
            "Panel.TLabel",
            background=palette["panel"],
            foreground=palette["text"],
        )
        self.style.configure(
            "PanelMuted.TLabel",
            background=palette["panel"],
            foreground=palette["secondary_text"],
        )
        self.style.configure(
            "PanelHeading.TLabel",
            background=palette["panel"],
            foreground=palette["secondary_text"],
            font=("Segoe UI", 10, "bold"),
        )
        self.style.configure(
            "Section.TLabel",
            background=palette["window"],
            foreground=palette["secondary_text"],
            font=("Segoe UI", 10, "bold"),
        )
        self.style.configure(
            "Metric.TLabel",
            background=palette["panel"],
            foreground=palette["text"],
            font=("Segoe UI", 10, "bold"),
        )
        self.style.configure(
            "Status.TLabel",
            background=palette["panel_alt"],
            foreground=palette["secondary_text"],
        )
        self.style.configure(
            "TButton",
            background=palette["button"],
            bordercolor=palette["border"],
            focusthickness=1,
            focuscolor=palette["focus"],
            foreground=palette["text"],
            padding=(12, 7),
        )
        self.style.map(
            "TButton",
            background=[
                ("pressed", palette["button_pressed"]),
                ("active", palette["button_hover"]),
                ("disabled", palette["panel"]),
            ],
            foreground=[("disabled", palette["disabled_text"])],
        )
        self.style.configure(
            "Primary.TButton",
            background=palette["primary"],
            bordercolor=palette["primary"],
            foreground=palette["primary_text"],
            padding=(14, 7),
        )
        self.style.map(
            "Primary.TButton",
            background=[
                ("pressed", palette["primary_hover"]),
                ("active", palette["primary_hover"]),
                ("disabled", palette["panel"]),
            ],
            foreground=[
                ("disabled", palette["disabled_text"]),
                ("!disabled", palette["primary_text"]),
            ],
        )
        self.style.configure(
            "TEntry",
            fieldbackground=palette["input"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            insertcolor=palette["text"],
            padding=(8, 7),
        )
        self.style.map(
            "TEntry",
            bordercolor=[("focus", palette["focus"])],
            fieldbackground=[
                ("readonly", palette["input"]),
                ("disabled", palette["panel_alt"]),
            ],
            foreground=[("disabled", palette["disabled_text"])],
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=palette["input"],
            background=palette["button"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            arrowcolor=palette["text"],
            padding=(8, 6),
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["input"])],
            foreground=[("disabled", palette["disabled_text"])],
            bordercolor=[("focus", palette["focus"])],
        )
        self.style.configure(
            "Horizontal.TProgressbar",
            background=palette["progress"],
            troughcolor=palette["panel_alt"],
            bordercolor=palette["border"],
            lightcolor=palette["progress"],
            darkcolor=palette["progress"],
        )

        self.log_text.configure(
            background=palette["log_bg"],
            foreground=palette["log_info"],
            insertbackground=palette["text"],
            selectbackground=palette["selection_bg"],
            selectforeground=palette["selection_text"],
            highlightbackground=palette["border"],
            highlightcolor=palette["focus"],
            borderwidth=1,
        )
        self.log_text.tag_configure("INFO", foreground=palette["log_info"])
        self.log_text.tag_configure("WARNING", foreground=palette["log_warning"])
        self.log_text.tag_configure("ERROR", foreground=palette["log_error"])

        for widget in self.tk_theme_widgets:
            parent_bg = palette["panel"]
            widget.configure(
                background=parent_bg,
                foreground=palette["text"],
                activebackground=parent_bg,
                activeforeground=palette["text"],
                disabledforeground=palette["disabled_text"],
                highlightbackground=parent_bg,
                highlightcolor=palette["focus"],
                selectcolor=palette["indicator"],
            )
        for tip in self.tooltip_widgets:
            tip.configure(background=palette["panel"])

        apply_windows_title_bar_theme(
            self.root,
            dark=resolved_theme == "dark",
            palette=palette,
        )

    def _resolved_theme(self) -> str:
        selected = self._normalise_theme(self.theme_var.get())
        if selected == "system":
            return detect_windows_theme()
        return selected

    def _normalise_theme(self, theme: str) -> str:
        theme_lower = theme.lower()
        return theme_lower if theme_lower in THEME_OPTIONS else "system"

    def _use_customisable_ttk_theme(self) -> None:
        available_themes = self.style.theme_names()
        if "clam" in available_themes:
            self.style.theme_use("clam")

    def _load_window_icons(self) -> dict[str, tk.PhotoImage]:
        icons: dict[str, tk.PhotoImage] = {}
        path = APP_DIR / "icon_light.png"
        if path.exists():
            try:
                icon = tk.PhotoImage(file=str(path))
                icons["light"] = icon
                icons["dark"] = icon
            except tk.TclError as exc:
                self.logger.warning("Could not load window icon %s: %s", path, exc)
        return icons

    def _apply_window_icon(self, resolved_theme: str) -> None:
        icon = self.window_icons.get("light") or self.window_icons.get(resolved_theme)
        if icon is None:
            return
        try:
            self.root.iconphoto(True, icon)
        except tk.TclError as exc:
            self.logger.debug("Could not apply window icon: %s", exc)

    def _enable_input_drop_target(self) -> None:
        if DND_FILES is None or not hasattr(self.input_entry, "drop_target_register"):
            return
        for widget in (self.input_entry,):
            try:
                widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
                widget.dnd_bind("<<Drop>>", self._on_input_drop)  # type: ignore[attr-defined]
            except tk.TclError as exc:
                self.logger.debug("Could not enable drag and drop: %s", exc)

    def _on_input_drop(self, event: tk.Event[Any]) -> None:
        paths = self._paths_from_drop_data(getattr(event, "data", ""))
        if not paths:
            return

        if self.mode_var.get() == "watch":
            folder = self._folder_from_drop(paths)
            if folder is None:
                self.status_var.set("Drop a folder for Watch Folder mode")
                return
            self.watch_input_folder = str(folder)
            self.input_var.set(str(folder))
            self._save_config()
            self.status_var.set("Input folder selected")
            return

        files = [
            path
            for path in paths
            if path.is_file() and path.suffix.lower() == ".msg"
        ]
        if not files:
            self.status_var.set("Drop one or more .msg files")
            return
        self._set_selected_input_files(files)
        self.status_var.set(f"{len(files)} file(s) selected")

    def _paths_from_drop_data(self, data: str) -> list[Path]:
        try:
            raw_paths = self.root.tk.splitlist(data)
        except tk.TclError:
            raw_paths = data.split()
        return [Path(raw_path) for raw_path in raw_paths if raw_path]

    def _folder_from_drop(self, paths: list[Path]) -> Path | None:
        for path in paths:
            if path.is_dir():
                return path
            if path.is_file():
                return path.parent
        return None

    def _set_selected_input_files(self, files: list[Path]) -> None:
        self.selected_input_files = [file.expanduser() for file in files]
        self.selected_input_display = self._format_selected_files(
            self.selected_input_files
        )
        self.input_var.set(self.selected_input_display)

    def _format_selected_files(self, files: list[Path]) -> str:
        return "; ".join(str(file) for file in files)

    def _selected_conversion_files(self) -> list[Path]:
        if (
            self.selected_input_files
            and self.input_var.get() == self.selected_input_display
        ):
            return self.selected_input_files

        value = self.input_var.get().strip()
        if not value:
            return []
        candidate = Path(value).expanduser()
        if candidate.is_file() and candidate.suffix.lower() == ".msg":
            return [candidate]
        return []

    def _initial_input_dir(self) -> str:
        if self.selected_input_files:
            return str(self.selected_input_files[0].parent)

        value = self.input_var.get().strip()
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_file():
                return str(candidate.parent)
            if candidate.is_dir():
                return str(candidate)

        watch_folder = Path(self.watch_input_folder).expanduser()
        if watch_folder.exists():
            return str(watch_folder)
        return str(APP_DIR)

    def _load_login_startup_state(self) -> bool:
        return is_watcher_startup_enabled()

    def browse_input(self) -> None:
        if self.mode_var.get() == "convert":
            files = filedialog.askopenfilenames(
                title="Select MSG files",
                initialdir=self._initial_input_dir(),
                filetypes=(
                    ("Outlook message files", "*.msg"),
                    ("All files", "*.*"),
                ),
            )
            if files:
                self._set_selected_input_files([Path(file) for file in files])
            return

        folder = filedialog.askdirectory(initialdir=self._initial_input_dir())
        if folder:
            self.watch_input_folder = folder
            self.input_var.set(folder)
            self._save_config()
            self._restart_watching_for_path_change()

    def browse_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)
            self._save_config()
            self._restart_watching_for_path_change()

    def _on_watch_path_entry_change(self, _event: tk.Event[Any]) -> None:
        if self.mode_var.get() != "watch":
            return
        if self.input_var.get().strip():
            self.watch_input_folder = self.input_var.get()
        self._save_config()
        self._restart_watching_for_path_change()

    def _restart_watching_for_path_change(self) -> None:
        if not self.watcher or not self.watcher.is_running:
            return
        if not self._watcher_paths_changed():
            return

        was_paused = self.watcher.is_paused
        self.status_var.set("Restarting watcher for changed folders")
        self.watcher.stop()
        self.watcher = None
        self.active_watch_input_folder = None
        self.active_watch_output_folder = None
        self.start_watching()
        if was_paused and self.watcher and self.watcher.is_running:
            self.watcher.pause()
            self.status_var.set("Watcher paused")

    def _watcher_paths_changed(self) -> bool:
        if self.active_watch_input_folder is None or self.active_watch_output_folder is None:
            return True
        try:
            input_folder = Path(self.input_var.get()).expanduser().resolve()
            output_folder = Path(self.output_var.get()).expanduser().resolve()
        except OSError:
            return True
        return (
            input_folder != self.active_watch_input_folder
            or output_folder != self.active_watch_output_folder
        )

    def start_conversion(self) -> None:
        if self.batch_running:
            return

        try:
            input_files = self._selected_conversion_files()
            output_folder = Path(self.output_var.get()).expanduser()
            if not input_files:
                raise ValueError("Select one or more .msg files to convert.")
            missing_files = [
                path for path in input_files if not path.expanduser().exists()
            ]
            if missing_files:
                raise FileNotFoundError(
                    f"Input file does not exist: {missing_files[0]}"
                )
            output_folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Cannot start conversion", str(exc))
            return

        self.mode_var.set("convert")
        self._save_config()
        self._reset_stats()
        self.cancel_event.clear()
        self.batch_running = True
        self.status_var.set("Processing file")
        self.current_file_var.set("Preparing conversion run...")
        self.primary_button.configure(state="disabled")
        self.cancel_button.grid()
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1)
        self.progress_var.set(0)

        callbacks = ConversionCallbacks(
            on_batch_start=lambda total: self.ui_queue.put(("batch_start", total)),
            on_file_start=lambda path: self.ui_queue.put(("file_start", path)),
            on_file_complete=lambda result: self.ui_queue.put(
                ("file_complete", result)
            ),
            on_batch_complete=lambda stats: self.ui_queue.put(
                ("batch_complete", stats)
            ),
            should_cancel=self.cancel_event.is_set,
        )
        self.worker_thread = threading.Thread(
            target=self._run_conversion,
            args=(input_files, output_folder, self._options_from_ui(), callbacks),
            name="msg-to-md-batch",
            daemon=True,
        )
        self.worker_thread.start()

    def _run_conversion(
        self,
        input_files: list[Path],
        output_folder: Path,
        options: ConversionOptions,
        callbacks: ConversionCallbacks,
    ) -> None:
        try:
            self.converter.convert_files(input_files, output_folder, options, callbacks)
        except Exception as exc:
            self.logger.exception("Conversion run failed")
            self.ui_queue.put(("batch_error", exc))

    def cancel_conversion(self) -> None:
        if self.batch_running:
            self.cancel_event.set()
            self.status_var.set("Cancelling after current file")
            self.current_file_var.set("Cancelling after the current file...")

    def start_watching(self) -> None:
        if self.watcher and self.watcher.is_running:
            return

        try:
            if (
                not self.input_var.get().strip()
                or self.input_var.get() == self.selected_input_display
            ):
                self.input_var.set(self.watch_input_folder)
            input_folder = Path(self.input_var.get()).expanduser()
            output_folder = Path(self.output_var.get()).expanduser()
            input_folder.mkdir(parents=True, exist_ok=True)
            output_folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Cannot start watching", str(exc))
            return

        self.mode_var.set("watch")
        self.watch_input_folder = str(input_folder)
        self._save_config()
        callbacks = WatcherCallbacks(
            on_file_detected=lambda path: self.ui_queue.put(("watch_detected", path)),
            on_file_start=lambda path: self.ui_queue.put(("watch_start", path)),
            on_file_complete=lambda result: self.ui_queue.put(
                ("watch_complete", result)
            ),
            on_scan_complete=lambda queued, skipped: self.ui_queue.put(
                ("watch_scan_complete", queued, skipped)
            ),
            on_error=lambda message, exc: self.ui_queue.put(
                ("watch_error", message, exc)
            ),
        )
        self.watcher = FolderWatcher(
            input_folder=input_folder,
            output_folder=output_folder,
            options=self._options_from_ui(),
            converter=self.converter,
            logger=self.logger,
            callbacks=callbacks,
        )
        try:
            self.watcher.start()
        except Exception as exc:
            self.logger.exception("Could not start watcher")
            messagebox.showerror("Cannot start watching", str(exc))
            return

        self.active_watch_input_folder = input_folder.expanduser().resolve()
        self.active_watch_output_folder = output_folder.expanduser().resolve()
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1)
        self.progress_var.set(0)
        self.status_var.set("Watching input folder | 0 files queued")
        self.current_file_var.set("Watcher idle")
        self._refresh_mode_ui()
        self._refresh_tray_menu()

    def stop_watching(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.watcher = None
        self.active_watch_input_folder = None
        self.active_watch_output_folder = None
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1)
        self.progress_var.set(0)
        if not self.batch_running:
            self.status_var.set("Watcher stopped")
            self.current_file_var.set("No file selected")
        self._refresh_mode_ui()
        self._refresh_tray_menu()

    def pause_watching(self) -> None:
        if self.watcher and self.watcher.is_running:
            self.watcher.pause()
            self.status_var.set("Watcher paused")
            self._refresh_tray_menu()

    def resume_watching(self) -> None:
        if self.watcher and self.watcher.is_running:
            self.watcher.resume()
            self.status_var.set("Watching input folder | 0 files queued")
        else:
            self.start_watching()
        self._refresh_tray_menu()

    def open_output_folder(self) -> None:
        path = Path(self.output_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]

    def on_close(self) -> None:
        if self.minimize_tray_var.get() and not self.exiting:
            self._hide_to_tray()
            return
        self.exit_application()

    def exit_application(self) -> None:
        self.exiting = True
        if self.batch_running:
            self.cancel_event.set()
        if self.watcher:
            self.watcher.stop()
        self._save_config()
        self._stop_tray_icon()
        self.logger.info("Application shutdown")
        self.root.destroy()

    def _run_convert_now_from_tray(self) -> None:
        self._restore_from_tray()
        self.mode_var.set("convert")
        self._refresh_mode_ui()
        self.start_conversion()

    def _on_unmap(self, _event: tk.Event[Any]) -> None:
        if self.exiting or not self.minimize_tray_var.get():
            return
        if self.root.state() == "iconic":
            self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        self._save_config()
        if self._ensure_tray_icon():
            self.root.withdraw()
        else:
            self.root.iconify()

    def _restore_from_tray(self) -> None:
        self.root.deiconify()
        self.root.after(50, self.root.lift)
        self._stop_tray_icon()

    def _ensure_tray_icon(self) -> bool:
        if self.tray_icon is not None:
            return True

        try:
            import pystray
            from PIL import Image
        except Exception as exc:
            self.logger.warning("System tray unavailable: %s", exc)
            return False

        image = self._load_tray_image(Image)

        def schedule(callback: Any) -> None:
            self.root.after(0, callback)

        menu = self._build_tray_menu(pystray, schedule)
        self.tray_icon = pystray.Icon("msg_to_md", image, "MSG to Markdown", menu)
        self.tray_thread = threading.Thread(
            target=self.tray_icon.run,  # type: ignore[attr-defined]
            name="msg-to-md-tray",
            daemon=True,
        )
        self.tray_thread.start()
        return True

    def _build_tray_menu(self, pystray: Any, schedule: Any) -> Any:
        return pystray.Menu(
            pystray.MenuItem(
                "Open application",
                lambda _icon, _item: schedule(self._restore_from_tray),
            ),
            pystray.MenuItem(
                lambda _item: self._watcher_status_label(),
                lambda _icon, _item: None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda _item: self._watcher_toggle_label(),
                lambda _icon, _item: schedule(self._toggle_watching_from_tray),
            ),
            pystray.MenuItem(
                "Convert now",
                lambda _icon, _item: schedule(self._run_convert_now_from_tray),
            ),
            pystray.MenuItem(
                "Open output folder",
                lambda _icon, _item: schedule(self.open_output_folder),
            ),
            pystray.MenuItem(
                "Exit",
                lambda _icon, _item: schedule(self.exit_application),
            ),
        )

    def _watcher_status_label(self) -> str:
        if self.watcher and self.watcher.is_running:
            return "Status: Paused" if self.watcher.is_paused else "Status: Watching"
        return "Status: Not watching"

    def _watcher_toggle_label(self) -> str:
        if self.watcher and self.watcher.is_running:
            return "Resume watching" if self.watcher.is_paused else "Pause watching"
        return "Start watching"

    def _toggle_watching_from_tray(self) -> None:
        if self.watcher and self.watcher.is_running:
            if self.watcher.is_paused:
                self.resume_watching()
            else:
                self.pause_watching()
        else:
            self.mode_var.set("watch")
            self._refresh_mode_ui()
            self.start_watching()
        self._refresh_tray_menu()

    def _refresh_tray_menu(self) -> None:
        if self.tray_icon is None:
            return
        try:
            self.tray_icon.update_menu()  # type: ignore[attr-defined]
        except Exception as exc:
            self.logger.debug("Could not refresh tray menu: %s", exc)

    def _load_tray_image(self, image_module: Any) -> Any:
        icon_path = APP_DIR / "icon_light.png"
        try:
            with image_module.open(icon_path) as icon:
                return icon.convert("RGBA").resize((64, 64), image_module.LANCZOS)
        except Exception as exc:
            self.logger.warning("Could not load system tray icon %s: %s", icon_path, exc)

        image = image_module.new("RGBA", (64, 64), (255, 255, 255, 0))
        return image

    def _stop_tray_icon(self) -> None:
        if self.tray_icon is None:
            return
        try:
            self.tray_icon.stop()  # type: ignore[attr-defined]
        except Exception as exc:
            self.logger.debug("Could not stop tray icon: %s", exc)
        self.tray_icon = None

    def _schedule_queue_drain(self) -> None:
        self._drain_log_queue()
        self._drain_ui_queue()
        self.root.after(150, self._schedule_queue_drain)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log_line(line)

    def _append_log_line(self, line: str) -> None:
        at_bottom = self.log_text.yview()[1] >= 0.98
        severity = "INFO"
        if "| ERROR" in line or "| CRITICAL" in line:
            severity = "ERROR"
        elif "| WARNING" in line:
            severity = "WARNING"

        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n", severity)
        if at_bottom:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_log_scroll(self, _event: tk.Event[Any]) -> None:
        self.root.after(100, lambda: None)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self) -> None:
        text = self.log_text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Log copied to clipboard")

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_ui_event(event)

    def _handle_ui_event(self, event: UiEvent) -> None:
        event_type = event[0]
        if event_type == "batch_start":
            total = max(int(event[1]), 1)
            self.progress_bar.configure(mode="determinate", maximum=total)
            self.progress_var.set(0)
            self.current_file_var.set(f"{event[1]} file(s) queued")
            self.status_var.set(f"Processing file | 0 of {event[1]}")
        elif event_type == "file_start":
            self.current_file_var.set(str(event[1]))
            self.status_var.set("Processing file")
        elif event_type == "file_complete":
            result = event[1]
            self._record_result(result)
            self.progress_var.set(self.progress_var.get() + 1)
            self.status_var.set(
                f"Processing file | {self._stats['processed']} processed"
            )
            if result.status == ConversionStatus.FAILED:
                self.notifier.notify(
                    "MSG conversion failed",
                    f"{result.source_path.name}: {result.message}",
                )
        elif event_type == "batch_complete":
            self._complete_batch(event[1])
        elif event_type == "batch_error":
            self._fail_batch(event[1])
        elif event_type == "watch_detected":
            self.current_file_var.set(f"Detected: {event[1]}")
            self.status_var.set("Watching input folder | 1 file queued")
        elif event_type == "watch_start":
            self.progress_bar.configure(mode="determinate", maximum=1)
            self.progress_var.set(0)
            self.status_var.set("Processing file | Watcher active")
            self.current_file_var.set(str(event[1]))
        elif event_type == "watch_complete":
            result = event[1]
            self._record_result(result)
            self.progress_var.set(1)
            self.status_var.set("Watching input folder | 0 files queued")
            self.root.after(1200, self._reset_watch_progress_if_idle)
            if result.status == ConversionStatus.SUCCEEDED:
                self.notifier.notify(
                    "MSG converted",
                    f"Converted {result.source_path.name}",
                )
            elif result.status == ConversionStatus.FAILED:
                self.notifier.notify(
                    "MSG conversion failed",
                    f"{result.source_path.name}: {result.message}",
                )
        elif event_type == "watch_scan_complete":
            queued = int(event[1])
            skipped = int(event[2])
            if queued:
                self.status_var.set(
                    f"Watching input folder | {queued} file(s) queued from scan"
                )
                self.current_file_var.set(
                    f"Catch-up scan: {queued} queued, {skipped} already converted"
                )
            else:
                self.status_var.set("Watching input folder | 0 files queued")
                self.current_file_var.set(
                    f"Catch-up scan: {skipped} already converted"
                    if skipped
                    else "Catch-up scan: no MSG files found"
                )
        elif event_type == "watch_error":
            self.status_var.set("Watching input folder | Error logged")
            self.notifier.notify("Watch Folder error", str(event[1]))

    def _reset_watch_progress_if_idle(self) -> None:
        if self.mode_var.get() == "watch" and not self.batch_running:
            self.progress_var.set(0)
            if self.watcher and self.watcher.is_running:
                self.status_var.set("Watching input folder | 0 files queued")

    def _complete_batch(self, stats: ConversionStats) -> None:
        self.batch_running = False
        self.primary_button.configure(state="normal")
        self.cancel_button.grid_remove()
        self.status_var.set(
            "Conversion complete" if not stats.cancelled else "Conversion cancelled"
        )
        self.current_file_var.set(
            "Batch complete" if not stats.cancelled else "Cancelled"
        )
        message = (
            f"{stats.succeeded} succeeded, "
            f"{stats.failed} failed, "
            f"{stats.skipped} skipped."
        )
        self.notifier.notify("Conversion batch complete", message)
        if self.open_output_var.get() and not stats.cancelled:
            self.open_output_folder()
        self._refresh_mode_ui()

    def _fail_batch(self, exc: Exception) -> None:
        self.batch_running = False
        self.primary_button.configure(state="normal")
        self.cancel_button.grid_remove()
        self.status_var.set("Conversion failed")
        self.current_file_var.set("Conversion failed")
        self.notifier.notify("Conversion error", str(exc))
        messagebox.showerror("Conversion failed", str(exc))
        self._refresh_mode_ui()

    def _record_result(self, result: ConversionResult) -> None:
        self._stats["processed"] += 1
        self._stats[result.status] += 1
        self.processed_var.set(str(self._stats["processed"]))
        self.succeeded_var.set(str(self._stats[ConversionStatus.SUCCEEDED]))
        self.failed_var.set(str(self._stats[ConversionStatus.FAILED]))
        self.skipped_var.set(str(self._stats[ConversionStatus.SKIPPED]))

    def _reset_stats(self) -> None:
        self._stats = {
            ConversionStatus.SUCCEEDED: 0,
            ConversionStatus.FAILED: 0,
            ConversionStatus.SKIPPED: 0,
            "processed": 0,
        }
        self.processed_var.set("0")
        self.succeeded_var.set("0")
        self.failed_var.set("0")
        self.skipped_var.set("0")

    def _options_from_ui(self) -> ConversionOptions:
        return ConversionOptions(
            recursive=self.process_subfolders_var.get(),
            skip_existing=True,
            overwrite=False,
            existing_file_action="skip",
        )

    def _save_config(self) -> None:
        if self.mode_var.get() == "watch":
            self.watch_input_folder = self.input_var.get()
        self.config.input_folder = self.input_var.get()
        if self.mode_var.get() == "convert":
            self.config.input_folder = self.watch_input_folder
        self.config.output_folder = self.output_var.get()
        self.config.window_geometry = self.root.geometry()
        self.config.app_mode = self.mode_var.get()
        self.config.process_subfolders = self.process_subfolders_var.get()
        self.config.skip_already_converted = True
        self.config.overwrite_existing = False
        self.config.existing_file_action = "skip"
        self.config.open_output_when_finished = self.open_output_var.get()
        self.config.auto_start_watching = self.auto_watch_var.get()
        self.config.minimize_to_tray = self.minimize_tray_var.get()
        self.config.theme = self.theme_var.get()
        self.config.details_expanded = self.preferences_expanded
        self.config.run_watcher_at_login = self.login_startup_var.get()
        self.config.enable_desktop_notifications = self.notifications_var.get()
        self.config.save()


def detect_windows_theme() -> str:
    """Return the current Windows app theme preference when available."""

    try:
        import winreg

        key_path = (
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) else "dark"
    except Exception:
        return "light"


def get_work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """Return the usable desktop work area."""

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(
                0x0030,
                0,
                ctypes.byref(rect),
                0,
            )
            return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass

    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def apply_windows_title_bar_theme(
    root: tk.Tk,
    *,
    dark: bool,
    palette: dict[str, str],
) -> None:
    """Apply Windows title-bar colours when DWM supports it."""

    if os.name != "nt":
        return

    try:
        import ctypes

        root.update_idletasks()
        hwnd = int(root.winfo_id())
        parent_hwnd = int(ctypes.windll.user32.GetParent(hwnd))
        handles = {hwnd, parent_hwnd} - {0}
        dwmapi = ctypes.windll.dwmapi

        dark_value = ctypes.c_int(1 if dark else 0)
        for handle in handles:
            for attribute in (20, 19):
                dwmapi.DwmSetWindowAttribute(
                    handle,
                    attribute,
                    ctypes.byref(dark_value),
                    ctypes.sizeof(dark_value),
                )

            colours = {
                34: palette.get("border", "#d1d5db"),
                35: palette.get("title_bar", palette.get("panel", "#ffffff")),
                36: palette.get("title_text", palette.get("text", "#111827")),
            }
            for attribute, colour in colours.items():
                colour_value = ctypes.c_uint(_hex_to_colorref(colour))
                dwmapi.DwmSetWindowAttribute(
                    handle,
                    attribute,
                    ctypes.byref(colour_value),
                    ctypes.sizeof(colour_value),
                )
    except Exception:
        return


def _hex_to_colorref(colour: str) -> int:
    """Convert '#rrggbb' to a Windows COLORREF value."""

    value = colour.lstrip("#")
    if len(value) != 6:
        return 0
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return red | (green << 8) | (blue << 16)


def run_gui(
    config: AppConfig,
    logger: logging.Logger,
    *,
    start_minimized: bool = False,
    start_watching: bool = False,
) -> None:
    """Start the Tkinter GUI."""

    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    app = MsgToMarkdownApp(
        root=root,
        config=config,
        logger=logger,
        notifier=DesktopNotifier(enabled=config.enable_desktop_notifications),
        start_minimized=start_minimized,
        start_watching=start_watching,
    )
    root.mainloop()
    app.logger.info("GUI closed")
