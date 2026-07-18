from __future__ import annotations

import builtins
from datetime import datetime, timezone
from threading import Lock
from typing import Any

_ORIGINAL_PRINT = builtins.print
_PRINT_LOCK = Lock()
_ENABLED = False


def _now_timestamp() -> str:
    # Local timezone with milliseconds for human-readable latency tracing.
    return (
        datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


def _prefix_lines(message: str, timestamp: str) -> str:
    if not message:
        return message

    lines = message.splitlines(keepends=True)
    prefixed: list[str] = []
    for line in lines:
        stripped = line.strip("\r\n")
        if stripped:
            prefixed.append(f"[{timestamp}] {line}")
        else:
            prefixed.append(line)
    return "".join(prefixed)


def _timestamped_print(*args: Any, **kwargs: Any) -> None:
    if not args:
        with _PRINT_LOCK:
            _ORIGINAL_PRINT(*args, **kwargs)
        return

    sep = kwargs.get("sep", " ")
    message = sep.join(str(arg) for arg in args)
    timestamp = _now_timestamp()
    message = _prefix_lines(message, timestamp)

    with _PRINT_LOCK:
        _ORIGINAL_PRINT(message, **kwargs)


def enable_timestamped_print() -> None:
    global _ENABLED
    if _ENABLED:
        return
    builtins.print = _timestamped_print
    _ENABLED = True
