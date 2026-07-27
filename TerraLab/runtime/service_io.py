"""Blocking JSONL helpers used only inside worker processes."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator

from TerraLab.runtime.protocol import Envelope, ProtocolError, decode, encode


_WRITE_LOCK = threading.Lock()
_PROTOCOL_STREAM = getattr(sys.stdout, "buffer", sys.stdout)


def reserve_stdout_for_protocol() -> None:
    """Route incidental worker/library prints away from the JSONL channel."""

    sys.stdout = sys.stderr


def read_messages() -> Iterator[Envelope]:
    """Yield valid messages from stdin until the parent closes the pipe."""

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    for line in iter(stream.readline, b""):
        if not line:
            break
        try:
            yield decode(line)
        except ProtocolError as exc:
            write_stderr(f"protocol error: {exc}")


def write_message(message: Envelope) -> None:
    data = encode(message)
    with _WRITE_LOCK:
        stream = _PROTOCOL_STREAM
        try:
            stream.write(data)
        except TypeError:
            stream.write(data.decode("utf-8"))
        stream.flush()


def write_stderr(message: str) -> None:
    print(str(message), file=sys.stderr, flush=True)
