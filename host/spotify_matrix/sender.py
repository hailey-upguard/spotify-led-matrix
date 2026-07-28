"""Push frames to the ESP32 firmware over HTTP."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class PanelSender:
    def __init__(self, host: str, timeout: float = 5.0):
        # host is e.g. "ledmatrix.local" or "192.168.1.42" (no scheme).
        self._base = f"http://{host}"
        self._timeout = timeout
        self._session = requests.Session()

    def send_frame(self, frame: bytes) -> None:
        resp = self._session.post(
            f"{self._base}/frame",
            data=frame,
            headers={"Content-Type": "application/octet-stream"},
            timeout=self._timeout,
        )
        resp.raise_for_status()

    def set_brightness(self, value: int) -> None:
        value = max(0, min(255, int(value)))
        self._session.post(
            f"{self._base}/brightness", data=str(value), timeout=self._timeout
        )

    def set_auto_brightness(self) -> None:
        self._session.post(
            f"{self._base}/brightness", data="auto", timeout=self._timeout
        )

    def clear(self) -> None:
        self._session.post(f"{self._base}/clear", timeout=self._timeout)
