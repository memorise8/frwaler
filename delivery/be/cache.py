"""Tiny process-local TTL cache for expensive read-only dashboard aggregates."""
from __future__ import annotations

import copy
import threading
import time


class TTLCache:
    def __init__(self, ttl_seconds: float = 30):
        self.ttl_seconds = max(0, float(ttl_seconds))
        self._items = {}
        self._lock = threading.Lock()

    def get_or_create(self, key, creator):
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(key)
            if cached and now - cached[0] < self.ttl_seconds:
                return copy.deepcopy(cached[1])
            value = creator()
            self._items[key] = (now, copy.deepcopy(value))
            return value

    def clear(self):
        with self._lock:
            self._items.clear()
