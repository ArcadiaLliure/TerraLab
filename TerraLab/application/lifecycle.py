"""Framework-neutral application lifecycle management for TerraLab."""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


class ApplicationLifecycleManager:
    """Manages system initialization, startup, worker lifecycle, and clean shutdown."""

    def __init__(self) -> None:
        self._running = False
        self._closing = False
        self._on_start_callbacks: list[Callable[[], None]] = []
        self._on_stop_callbacks: list[Callable[[], None]] = []
        self._on_error_callbacks: list[Callable[[Exception], None]] = []

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_closing(self) -> bool:
        return self._closing

    def register_on_start(self, callback: Callable[[], None]) -> None:
        self._on_start_callbacks.append(callback)

    def register_on_stop(self, callback: Callable[[], None]) -> None:
        self._on_stop_callbacks.append(callback)

    def register_on_error(self, callback: Callable[[Exception], None]) -> None:
        self._on_error_callbacks.append(callback)

    def start(self) -> None:
        if self._running:
            return
        self._closing = False
        self._running = True
        logger.info("Application lifecycle starting...")
        for callback in self._on_start_callbacks:
            try:
                callback()
            except Exception as exc:
                logger.error("Error executing startup callback: %s", exc)
                self._notify_error(exc)

    def stop(self, timeout_ms: int = 5000) -> None:
        if not self._running and not self._closing:
            return
        self._closing = True
        self._running = False
        logger.info("Application lifecycle stopping (timeout_ms=%d)...", timeout_ms)
        for callback in reversed(self._on_stop_callbacks):
            try:
                callback()
            except Exception as exc:
                logger.error("Error executing shutdown callback: %s", exc)

    def restart_workers(self) -> None:
        logger.info("Restarting application workers...")
        self.stop()
        self.start()

    def _notify_error(self, exc: Exception) -> None:
        for callback in self._on_error_callbacks:
            try:
                callback(exc)
            except Exception as inner_exc:
                logger.error("Error in error notification callback: %s", inner_exc)
