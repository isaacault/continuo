"""A small in-memory TTL cache (catalogue lookups: SkyHook is rate-limited and slow)."""
import time
from typing import Any


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 256):
        self.ttl, self.max_items = ttl, max_items
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key):
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        self._data.pop(key, None)
        return None

    def put(self, key, value) -> None:
        if len(self._data) >= self.max_items:
            cutoff = time.monotonic() - self.ttl
            for k in [k for k, (ts, _) in self._data.items() if ts < cutoff]:
                del self._data[k]
            while len(self._data) >= self.max_items:
                del self._data[next(iter(self._data))]
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


async def cached(cache: TTLCache, key, fn):
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = await fn()
    cache.put(key, value)
    return value
