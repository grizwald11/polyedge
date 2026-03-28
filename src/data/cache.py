"""Simple TTL cache for data source clients.

In-memory cache with per-key expiration. Used by FRED, Cleveland Fed,
FedWatch, Metaculus, and Polymarket cross-ref clients to avoid
redundant API calls within a scan cycle.
"""

from __future__ import annotations

import time
from typing import Any, Optional


class TTLCache:
    """Thread-safe in-memory cache with per-key TTL expiration."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Get a cached value if it exists and hasn't expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return data

    def set(self, key: str, data: Any) -> None:
        """Store a value with TTL expiration."""
        self._store[key] = (time.monotonic() + self.ttl_seconds, data)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._store.clear()

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count of entries removed."""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]
        return len(expired)
