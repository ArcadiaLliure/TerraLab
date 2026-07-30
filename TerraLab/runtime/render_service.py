"""Stable compatibility entry point for the toolkit-neutral render runtime."""

from __future__ import annotations

from TerraLab.infrastructure.runtime import render_service as _implementation

_RenderMailbox = _implementation._RenderMailbox
write_message = _implementation.write_message


class OffscreenService(_implementation.OffscreenService):
    """Legacy import shim preserving injectable protocol output for tests."""

    def render_delta(self, payload, generation: int) -> None:
        # Historical callers monkey-patched this module's protocol writer.
        # Keep that seam while the implementation resides in infrastructure.
        _implementation.write_message = write_message
        super().render_delta(payload, generation)


def run() -> int:
    return _implementation.run()


__all__ = ("_RenderMailbox", "OffscreenService", "run")


if __name__ == "__main__":
    raise SystemExit(run())
