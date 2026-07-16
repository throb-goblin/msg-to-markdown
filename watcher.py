"""Watchdog-based folder monitoring for new Outlook .msg files."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from converter import (
    ConversionOptions,
    ConversionResult,
    MsgToMarkdownConverter,
    extract_msg_metadata,
    find_msg_files,
    is_temporary_or_unsupported,
    output_paths_for,
    output_stem_for,
    wait_for_stable_file,
)


@dataclass(slots=True)
class WatcherCallbacks:
    """Optional callbacks raised from the watch worker thread."""

    on_file_detected: Callable[[Path], None] | None = None
    on_file_start: Callable[[Path], None] | None = None
    on_file_complete: Callable[[ConversionResult], None] | None = None
    on_scan_complete: Callable[[int, int], None] | None = None
    on_error: Callable[[str, Exception | None], None] | None = None


class _MsgFileEventHandler:
    """Adapter object for watchdog file system events."""

    def __init__(self, enqueue: Callable[[Path], None]) -> None:
        from watchdog.events import FileSystemEventHandler

        class Handler(FileSystemEventHandler):
            def on_created(self, event: object) -> None:
                if not getattr(event, "is_directory", False):
                    enqueue(Path(str(getattr(event, "src_path"))))

            def on_moved(self, event: object) -> None:
                if not getattr(event, "is_directory", False):
                    enqueue(Path(str(getattr(event, "dest_path"))))

        self.handler = Handler()


class FolderWatcher:
    """Monitor a folder and convert new .msg files until stopped."""

    def __init__(
        self,
        input_folder: Path,
        output_folder: Path,
        options: ConversionOptions,
        converter: MsgToMarkdownConverter,
        logger: logging.Logger | None = None,
        callbacks: WatcherCallbacks | None = None,
    ) -> None:
        self._input_folder = input_folder.expanduser().resolve()
        self._output_folder = output_folder.expanduser().resolve()
        self._options = options
        self._converter = converter
        self._logger = logger or logging.getLogger("msg_to_md")
        self._callbacks = callbacks or WatcherCallbacks()
        self._queue: "queue.Queue[Path | None]" = queue.Queue()
        self._pending: set[Path] = set()
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._observer: object | None = None
        self._worker_thread: threading.Thread | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Return whether the watcher is active."""

        return self._running

    @property
    def is_paused(self) -> bool:
        """Return whether the watcher worker is paused."""

        return self._pause_event.is_set()

    def start(self) -> None:
        """Start watchdog and the conversion worker."""

        if self._running:
            return

        from watchdog.observers import Observer

        self._input_folder.mkdir(parents=True, exist_ok=True)
        self._output_folder.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._pause_event.clear()
        self._queue = queue.Queue()
        with self._pending_lock:
            self._pending.clear()

        event_handler = _MsgFileEventHandler(self._enqueue).handler
        observer = Observer()
        observer.schedule(
            event_handler,
            str(self._input_folder),
            recursive=self._options.recursive,
        )
        observer.start()
        self._observer = observer
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="msg-to-md-watch-worker",
            daemon=True,
        )
        self._worker_thread.start()
        self._running = True
        self._logger.info(
            "Watch Folder started: %s | recursive=%s",
            self._input_folder,
            self._options.recursive,
        )
        self.scan_existing()

    def stop(self) -> None:
        """Stop watching and wait briefly for worker shutdown."""

        if not self._running:
            return

        self._stop_event.set()
        self._queue.put(None)

        if self._observer is not None:
            self._observer.stop()  # type: ignore[attr-defined]
            self._observer.join(timeout=5)  # type: ignore[attr-defined]
            self._observer = None

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
            self._worker_thread = None

        self._running = False
        self._logger.info("Watch Folder stopped")

    def pause(self) -> None:
        """Pause conversions for detected files."""

        if self._running and not self._pause_event.is_set():
            self._pause_event.set()
            self._logger.info("Watch Folder paused")

    def resume(self) -> None:
        """Resume conversions for detected files."""

        if self._running and self._pause_event.is_set():
            self._pause_event.clear()
            self._logger.info("Watch Folder resumed")
            self.scan_existing()

    def scan_existing(self) -> tuple[int, int]:
        """Queue existing source files that do not already have Markdown output."""

        if not self._running:
            return (0, 0)

        queued = 0
        already_converted = 0
        for path in find_msg_files(self._input_folder, self._options.recursive):
            if self._stop_event.is_set():
                break
            if self._output_exists_for(path):
                already_converted += 1
                continue
            if self._enqueue(path, source="scan"):
                queued += 1

        self._logger.info(
            "Watch Folder catch-up scan complete: %s queued, %s already converted",
            queued,
            already_converted,
        )
        if self._callbacks.on_scan_complete:
            self._callbacks.on_scan_complete(queued, already_converted)
        return (queued, already_converted)

    def _enqueue(self, path: Path, source: str = "event") -> bool:
        if is_temporary_or_unsupported(path):
            return False

        resolved = path.expanduser().resolve()
        with self._pending_lock:
            if resolved in self._pending:
                return False
            self._pending.add(resolved)

        self._logger.info("Watch %s detected: %s", source, resolved)
        if self._callbacks.on_file_detected:
            self._callbacks.on_file_detected(resolved)
        self._queue.put(resolved)
        return True

    def _output_exists_for(self, path: Path) -> bool:
        metadata = extract_msg_metadata(path)
        output_stem = output_stem_for(path, metadata)
        output_paths = output_paths_for(
            path.expanduser().resolve(),
            self._input_folder,
            self._output_folder,
            output_stem=output_stem,
        )
        return output_paths.main_markdown.exists()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if path is None:
                break

            try:
                self._wait_while_paused()
                if self._stop_event.is_set():
                    break

                self._logger.info("Waiting for copy to finish: %s", path)
                if not wait_for_stable_file(path):
                    self._handle_error(
                        f"Timed out waiting for file copy to complete: {path}",
                        None,
                    )
                    continue

                if self._callbacks.on_file_start:
                    self._callbacks.on_file_start(path)

                result = self._converter.convert_file(
                    source_path=path,
                    input_root=self._input_folder,
                    output_folder=self._output_folder,
                    options=self._options,
                )
                if self._callbacks.on_file_complete:
                    self._callbacks.on_file_complete(result)
            except Exception as exc:
                self._logger.exception("Watch worker error for %s", path)
                self._handle_error(f"Watch worker error for {path}", exc)
            finally:
                with self._pending_lock:
                    self._pending.discard(path)

    def _wait_while_paused(self) -> None:
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.25)

    def _handle_error(self, message: str, exc: Exception | None) -> None:
        if exc is None:
            self._logger.error(message)
        else:
            self._logger.error("%s: %s", message, exc)
        if self._callbacks.on_error:
            self._callbacks.on_error(message, exc)
