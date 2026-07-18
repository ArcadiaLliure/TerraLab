from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from TerraLab.common.app_paths import app_root


def perf_log_path() -> Path:
    path = app_root() / "logs" / "terralab_perf.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_perf_event(event: str, **payload: Any) -> None:
    record = {
        "ts": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "mono_ms": int(time.perf_counter() * 1000.0),
        "event": str(event),
    }
    for key, value in payload.items():
        if value is None:
            continue
        record[str(key)] = value

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with perf_log_path().open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        # Telemetry must never break runtime execution.
        pass
