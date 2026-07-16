"""Application configuration for MSG to Markdown."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_FOLDER = APP_DIR / "input_folder"
DEFAULT_OUTPUT_FOLDER = APP_DIR / "output_folder"
DEFAULT_LOG_FOLDER = APP_DIR / "logs"
CONFIG_FILE = APP_DIR / "config.json"
THEME_MODES = {"system", "light", "dark"}
EXISTING_FILE_ACTIONS = {"skip", "replace"}
APP_MODES = {"convert", "watch"}


@dataclass(slots=True)
class AppConfig:
    """User-editable application settings persisted to JSON."""

    input_folder: str = str(DEFAULT_INPUT_FOLDER)
    output_folder: str = str(DEFAULT_OUTPUT_FOLDER)
    window_geometry: str = "1120x780"
    process_subfolders: bool = True
    skip_already_converted: bool = True
    overwrite_existing: bool = False
    existing_file_action: str = "skip"
    open_output_when_finished: bool = True
    auto_start_watching: bool = False
    minimize_to_tray: bool = True
    theme: str = "system"
    details_expanded: bool = False
    app_mode: str = "convert"
    run_watcher_at_login: bool = False
    enable_desktop_notifications: bool = False

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "AppConfig":
        """Load configuration from disk, returning defaults on first run."""

        if not path.exists():
            config = cls()
            config.ensure_folders()
            return config

        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.getLogger(__name__).warning(
                "Could not read config file %s: %s. Using defaults.",
                path,
                exc,
            )
            config = cls()
            config.ensure_folders()
            return config

        valid_names = {field.name for field in fields(cls)}
        filtered: dict[str, Any] = {
            key: value for key, value in raw_data.items() if key in valid_names
        }
        config = cls(**filtered)
        if config.theme not in THEME_MODES:
            config.theme = "system"
        if config.existing_file_action not in EXISTING_FILE_ACTIONS:
            if config.overwrite_existing:
                config.existing_file_action = "replace"
            else:
                config.existing_file_action = "skip"
        if config.app_mode not in APP_MODES:
            config.app_mode = "convert"
        config.ensure_folders()
        return config

    def save(self, path: Path = CONFIG_FILE) -> None:
        """Persist configuration to a readable JSON file."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def ensure_folders(self) -> None:
        """Create configured input, output and log folders if needed."""

        Path(self.input_folder).mkdir(parents=True, exist_ok=True)
        Path(self.output_folder).mkdir(parents=True, exist_ok=True)
        DEFAULT_LOG_FOLDER.mkdir(parents=True, exist_ok=True)
