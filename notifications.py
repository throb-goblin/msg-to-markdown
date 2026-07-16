"""Windows desktop notification helpers."""

from __future__ import annotations

import logging


class DesktopNotifier:
    """Desktop notification facade with a conservative default."""

    def __init__(
        self,
        app_name: str = "MSG to Markdown",
        enabled: bool = False,
    ) -> None:
        self._app_name = app_name
        self._enabled = enabled
        self._logger = logging.getLogger("msg_to_md")

    @property
    def enabled(self) -> bool:
        """Return whether native Windows desktop notifications are enabled."""

        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def notify(self, title: str, message: str) -> None:
        """Show a Windows notification if winotify is available."""

        if not self._enabled:
            self._logger.info("%s: %s", title, message)
            return

        try:
            from winotify import Notification

            toast = Notification(app_id=self._app_name, title=title, msg=message)
            toast.show()
        except Exception as exc:
            self._logger.debug("Desktop notification unavailable: %s", exc)
