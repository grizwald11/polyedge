"""Tests for the TTL cache utility."""

import time
from unittest.mock import patch

from src.data.cache import TTLCache


class TestTTLCache:
    def test_set_and_get(self):
        cache = TTLCache(ttl_seconds=60)
        cache.set("key1", {"value": 42})
        assert cache.get("key1") == {"value": 42}

    def test_get_missing_key(self):
        cache = TTLCache(ttl_seconds=60)
        assert cache.get("nonexistent") is None

    def test_expired_key_returns_none(self):
        cache = TTLCache(ttl_seconds=1)
        cache.set("key1", "data")

        # Manually expire the entry
        cache._store["key1"] = (time.monotonic() - 1, "data")
        assert cache.get("key1") is None

    def test_overwrite_key(self):
        cache = TTLCache(ttl_seconds=60)
        cache.set("key1", "old")
        cache.set("key1", "new")
        assert cache.get("key1") == "new"

    def test_clear(self):
        cache = TTLCache(ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_different_ttls(self):
        short = TTLCache(ttl_seconds=1)
        long = TTLCache(ttl_seconds=3600)
        short.set("k", "v")
        long.set("k", "v")

        # Expire short cache
        short._store["k"] = (time.monotonic() - 1, "v")
        assert short.get("k") is None
        assert long.get("k") == "v"
