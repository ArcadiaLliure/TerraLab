"""Triple-buffered shared images for the render/UI process boundary."""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Iterable


BYTES_PER_PIXEL = 4
DEFAULT_SLOT_COUNT = 3


@dataclass(frozen=True, slots=True)
class FramePoolDescriptor:
    """Serializable description of a shared RGBA frame pool."""

    names: tuple[str, ...]
    width: int
    height: int
    stride: int
    pixel_format: str = "argb32_premultiplied"

    @property
    def slot_bytes(self) -> int:
        return int(self.stride) * int(self.height)

    def to_payload(self) -> dict[str, object]:
        return {
            "names": list(self.names),
            "width": int(self.width),
            "height": int(self.height),
            "stride": int(self.stride),
            "pixel_format": str(self.pixel_format),
        }

    @classmethod
    def from_payload(cls, payload) -> "FramePoolDescriptor":
        names = tuple(str(name) for name in payload.get("names", ()))
        descriptor = cls(
            names=names,
            width=int(payload.get("width", 0)),
            height=int(payload.get("height", 0)),
            stride=int(payload.get("stride", 0)),
            pixel_format=str(
                payload.get("pixel_format", "argb32_premultiplied")
            ),
        )
        descriptor.validate()
        return descriptor

    def validate(self) -> None:
        if len(self.names) < 2 or len(set(self.names)) != len(self.names):
            raise ValueError("Frame pool needs at least two unique slots")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Frame dimensions must be positive")
        if self.stride < self.width * BYTES_PER_PIXEL:
            raise ValueError("Frame stride is too small")
        if self.pixel_format != "argb32_premultiplied":
            raise ValueError(f"Unsupported pixel format: {self.pixel_format}")


class OwnedFramePool:
    """UI-owned shared memory with deterministic cleanup."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        slot_count: int = DEFAULT_SLOT_COUNT,
    ) -> None:
        width = max(1, int(width))
        height = max(1, int(height))
        stride = width * BYTES_PER_PIXEL
        count = max(2, int(slot_count))
        self._segments = tuple(
            shared_memory.SharedMemory(create=True, size=stride * height)
            for _ in range(count)
        )
        self.descriptor = FramePoolDescriptor(
            names=tuple(segment.name for segment in self._segments),
            width=width,
            height=height,
            stride=stride,
        )
        self._closed = False

    def buffer(self, slot: int) -> memoryview:
        # ``SharedMemory.buf`` is the segment's owning memoryview. Releasing
        # that object makes every later access fail with "operation forbidden
        # on released memoryview". Give each paint/render call an independent
        # view so callers can deterministically release only their lease.
        return memoryview(self._segments[int(slot)].buf)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for segment in self._segments:
            segment.close()
        for segment in self._segments:
            try:
                segment.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> "OwnedFramePool":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class AttachedFramePool:
    """Worker-side view of memory owned by the UI."""

    def __init__(self, descriptor: FramePoolDescriptor) -> None:
        descriptor.validate()
        self.descriptor = descriptor
        self._segments = tuple(
            _attach(name)
            for name in descriptor.names
        )

    def buffer(self, slot: int) -> memoryview:
        return memoryview(self._segments[int(slot)].buf)

    def close(self) -> None:
        segments, self._segments = self._segments, ()
        for segment in segments:
            segment.close()

    def __enter__(self) -> "AttachedFramePool":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def first_free_slot(
    slot_count: int,
    busy_slots: Iterable[int],
    *,
    after: int = -1,
) -> int | None:
    """Choose a free slot round-robin without overwriting an owned frame."""

    count = max(0, int(slot_count))
    if count == 0:
        return None
    busy = {int(slot) for slot in busy_slots}
    for offset in range(1, count + 1):
        candidate = (int(after) + offset) % count
        if candidate not in busy:
            return candidate
    return None


def _attach(name: str) -> shared_memory.SharedMemory:
    """Attach without letting an independent worker unlink UI-owned memory."""

    try:
        return shared_memory.SharedMemory(
            name=name,
            create=False,
            track=False,
        )
    except TypeError:  # Python 3.10-3.12
        return shared_memory.SharedMemory(name=name, create=False)
