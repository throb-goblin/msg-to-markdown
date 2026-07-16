"""Windows login startup registration for the watcher."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "MSG to Markdown Watcher"


class StartupRegistrationError(RuntimeError):
    """Raised when the Windows login startup setting cannot be changed."""


def build_startup_command(
    python_executable: Path | None = None,
    app_path: Path | None = None,
) -> str:
    """Build the command stored in the per-user Windows Run key."""

    executable = python_executable or _pythonw_executable()
    script = app_path or Path(__file__).resolve().parent / "app.py"
    return subprocess.list2cmdline(
        [
            str(executable),
            str(script),
            "--gui",
            "--start-minimized",
            "--watch-on-launch",
        ]
    )


def enable_watcher_startup() -> None:
    """Register this application to start the watcher at Windows login."""

    try:
        import winreg

        command = build_startup_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(
                key,
                STARTUP_VALUE_NAME,
                0,
                winreg.REG_SZ,
                command,
            )
    except Exception as exc:
        raise StartupRegistrationError(
            f"Could not enable Windows login startup: {exc}"
        ) from exc


def disable_watcher_startup() -> None:
    """Remove this application's Windows login startup registration."""

    try:
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            )
        except FileNotFoundError:
            return

        with key:
            winreg.DeleteValue(key, STARTUP_VALUE_NAME)
    except FileNotFoundError:
        return
    except Exception as exc:
        raise StartupRegistrationError(
            f"Could not disable Windows login startup: {exc}"
        ) from exc


def is_watcher_startup_enabled() -> bool:
    """Return whether the watcher is registered for Windows login startup."""

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _pythonw_executable() -> Path:
    """Prefer pythonw.exe so login startup does not show a console window."""

    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else executable
