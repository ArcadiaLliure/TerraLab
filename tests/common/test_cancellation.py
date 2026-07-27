from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from TerraLab.common.cancellation import (
    CancelledGeneration,
    GenerationController,
)


def test_new_generation_cancels_previous_token():
    controller = GenerationController()
    first = controller.next()
    second = controller.next()
    assert first.cancelled
    assert not second.cancelled
    with pytest.raises(CancelledGeneration):
        first.raise_if_cancelled()


def test_generation_controller_is_monotonic_under_concurrency():
    controller = GenerationController()
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _index: controller.next(), range(100)))
    generations = sorted(token.generation for token in tokens)
    assert generations == list(range(1, 101))
    assert sum(not token.cancelled for token in tokens) == 1
