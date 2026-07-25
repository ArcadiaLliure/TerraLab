from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from TerraLab.common.cache import ByteLRU


def test_byte_lru_tracks_hits_misses_replacement_and_eviction():
    cache: ByteLRU[str, str] = ByteLRU(max_bytes=10)
    cache.put("a", "A", 4)
    cache.put("a", "AA", 6)
    assert cache.resident_bytes == 6
    assert cache.get("missing") is None
    assert cache.get("a") == "AA"
    cache.put("b", "B", 6)
    assert cache.get("a") is None
    assert cache.get("b") == "B"
    assert cache.hits == 2
    assert cache.misses == 2
    assert cache.evictions == 1


def test_byte_lru_is_safe_under_parallel_get_and_put():
    cache: ByteLRU[int, int] = ByteLRU(max_bytes=128)

    def operate(seed: int) -> None:
        for offset in range(200):
            key = (seed + offset) % 32
            cache.put(key, offset, 4)
            cache.get(key)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(operate, range(8)))

    assert 0 <= cache.resident_bytes <= cache.max_bytes
    assert cache.hits > 0


def test_byte_lru_evicts_by_resident_bytes():
    cache = ByteLRU(max_bytes=10)
    cache.put("a", "A", 6)
    cache.put("b", "B", 6)
    assert cache.get("a") is None
    assert cache.get("b") == "B"
    assert cache.resident_bytes == 6
    assert cache.evictions == 1
