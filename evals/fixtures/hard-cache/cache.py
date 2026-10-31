"""容量 + TTL 缓存（初始实现：TTL 不生效、淘汰顺序错误、统计不准）。"""

from __future__ import annotations

from typing import Callable


class TTLCache:
    def __init__(self, capacity: int, ttl: float, clock: Callable[[], float]) -> None:
        self.capacity = capacity
        self.ttl = ttl
        self.clock = clock
        self.data: dict[str, tuple[float, float]] = {}
        self.order: list[str] = []
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    def get(self, key: str) -> float | None:
        if key in self.data:
            self.hits += 1
            return self.data[key][0]
        self.misses += 1
        return None

    def put(self, key: str, value: float) -> None:
        self.data[key] = (value, self.clock() + self.ttl)
        if key not in self.order:
            self.order.append(key)
        while len(self.order) > self.capacity:
            oldest = self.order.pop(0)
            self.data.pop(oldest, None)
            self.evictions += 1

    def stats(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "expirations": self.expirations,
        }
