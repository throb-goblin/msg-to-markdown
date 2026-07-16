"""Entry point for the MSG to Markdown desktop and CLI application."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import APP_DIR, DEFAULT_LOG_FOLDER, AppConfig
from converter import (
    ConversionCallbacks,
    ConversionOptions,
    ConversionResult,
    ConversionStats,
    MsgToMarkdownConverter,
)
from gui import run_gui
from logger import setup_logging
from notifications import DesktopNotifier
from watcher import FolderWatcher, WatcherCallbacks


def build_parser() -> argparse.ArgumentParser:
    """Create the command line parser."""

    parser = argparse.ArgumentParser(
        description="Convert Microsoft Outlook .msg files to Markdown.",
    )
    parser.add_argument(
        "--input",
        "-i",
        help="Input .msg file or folder. Defaults to the saved input folder.",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output folder. Defaults to the saved output folder.",
    )
    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="Process subfolders recursively.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing Markdown files.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously watch the input folder for new .msg files.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the desktop GUI even when other startup flags are present.",
    )
    parser.add_argument(
        "--start-minimized",
        action="store_true",
        help="Start the GUI minimised to the notification area.",
    )
    parser.add_argument(
        "--watch-on-launch",
        action="store_true",
        help="Start Watch Folder mode when the GUI opens.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run GUI by default, or CLI when arguments are supplied."""

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        if relaunch_gui_with_pythonw(argv):
            return 0
        config = AppConfig.load()
        logger, _log_path = setup_logging(DEFAULT_LOG_FOLDER)
        run_gui(config, logger)
        logging.shutdown()
        return 0

    args = build_parser().parse_args(argv)
    if args.gui or args.start_minimized or args.watch_on_launch:
        if relaunch_gui_with_pythonw(argv):
            return 0

    config = AppConfig.load()
    logger, _log_path = setup_logging(DEFAULT_LOG_FOLDER)
    try:
        if args.gui or args.start_minimized or args.watch_on_launch:
            apply_gui_overrides(args, config)
            run_gui(
                config,
                logger,
                start_minimized=args.start_minimized,
                start_watching=args.watch_on_launch,
            )
        elif args.watch:
            run_watch_cli(args, config, logger)
        else:
            stats = run_conversion_cli(args, config, logger)
            return 1 if stats.failed else 0
    finally:
        logging.shutdown()
    return 0


def relaunch_gui_with_pythonw(argv: list[str]) -> bool:
    """Relaunch GUI sessions with pythonw.exe so no console stays attached."""

    if os.name != "nt":
        return False
    if os.environ.get("MSG_TO_MD_PYTHONW_RELAUNCHED") == "1":
        return False

    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        return False

    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.exists():
        return False

    env = os.environ.copy()
    env["MSG_TO_MD_PYTHONW_RELAUNCHED"] = "1"
    command = [str(pythonw), str(APP_DIR / "app.py"), *argv]
    creation_flags = 0
    for flag_name in (
        "CREATE_NO_WINDOW",
        "DETACHED_PROCESS",
        "CREATE_NEW_PROCESS_GROUP",
    ):
        creation_flags |= getattr(subprocess, flag_name, 0)

    subprocess.Popen(
        command,
        cwd=str(APP_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    return True


def apply_gui_overrides(args: argparse.Namespace, config: AppConfig) -> None:
    """Apply optional CLI values before opening the GUI."""

    if args.input:
        config.input_folder = args.input
    if args.output:
        config.output_folder = args.output
    if args.recursive:
        config.process_subfolders = True
    if args.force:
        config.overwrite_existing = True
        config.skip_already_converted = False
        config.existing_file_action = "replace"


def run_conversion_cli(
    args: argparse.Namespace,
    config: AppConfig,
    logger: logging.Logger,
) -> ConversionStats:
    """Run one CLI conversion batch."""

    input_path = Path(args.input or config.input_folder)
    output_folder = Path(args.output or config.output_folder)
    options = ConversionOptions(
        recursive=args.recursive,
        skip_existing=not args.force,
        overwrite=args.force,
        existing_file_action="replace" if args.force else "skip",
    )
    converter = MsgToMarkdownConverter(logger=logger)

    callbacks = ConversionCallbacks(
        on_batch_start=lambda total: print(f"Queued {total} file(s)."),
        on_file_start=lambda path: print(f"Processing: {path}"),
        on_file_complete=_print_result,
    )
    stats = converter.convert_path(input_path, output_folder, options, callbacks)
    print()
    for line in stats.summary_lines():
        print(line)

    DesktopNotifier().notify(
        "Conversion batch complete",
        f"{stats.succeeded} succeeded, {stats.failed} failed, {stats.skipped} skipped.",
    )
    return stats


def run_watch_cli(
    args: argparse.Namespace,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    """Run the CLI watch service until Ctrl+C."""

    input_folder = Path(args.input or config.input_folder)
    output_folder = Path(args.output or config.output_folder)
    options = ConversionOptions(
        recursive=args.recursive,
        skip_existing=not args.force,
        overwrite=args.force,
        existing_file_action="replace" if args.force else "skip",
    )
    converter = MsgToMarkdownConverter(logger=logger)
    notifier = DesktopNotifier()

    callbacks = WatcherCallbacks(
        on_file_detected=lambda path: print(f"Detected: {path}"),
        on_file_start=lambda path: print(f"Processing: {path}"),
        on_file_complete=lambda result: _print_watch_result(result, notifier),
        on_error=lambda message, exc: notifier.notify("Watch Folder error", message),
    )
    watcher = FolderWatcher(
        input_folder=input_folder,
        output_folder=output_folder,
        options=options,
        converter=converter,
        logger=logger,
        callbacks=callbacks,
    )

    stop_requested = False

    def handle_signal(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    watcher.start()
    print(f"Watching {input_folder}. Press Ctrl+C to stop.")
    try:
        while not stop_requested:
            time.sleep(0.5)
    finally:
        watcher.stop()
        logger.info("CLI watch stopped")


def _print_result(result: ConversionResult) -> None:
    output = result.output_path if result.output_path else ""
    print(f"{result.status.value.upper():9s} {result.source_path} {output}")
    if result.message and result.status.value != "succeeded":
        print(f"          {result.message}")


def _print_watch_result(result: ConversionResult, notifier: DesktopNotifier) -> None:
    _print_result(result)
    if result.status.value == "succeeded":
        notifier.notify("MSG converted", f"Converted {result.source_path.name}")
    elif result.status.value == "failed":
        notifier.notify(
            "MSG conversion failed",
            f"{result.source_path.name}: {result.message}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
