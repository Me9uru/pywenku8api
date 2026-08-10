"""Bounded in-memory async cache used by :mod:`wenku8.api`."""

from __future__ import annotations

import asyncio
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
from typing import Awaitable, Callable, Generic, TypeVar


T = TypeVar("T")
CacheId = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Fresh/stale lifetime for one kind of value."""

    fresh_ttl: float
    stale_ttl: float = 0
    stale_while_revalidate: bool = True


@dataclass(slots=True)
class CacheEntry(Generic[T]):
    value: T
    fresh_until: float
    stale_until: float
    size_bytes: int

    def is_fresh(self, now: float) -> bool:
        return now < self.fresh_until

    def is_stale_usable(self, now: float) -> bool:
        return now < self.stale_until


class MemoryCache:
    """A small byte-weighted LRU cache.

    Methods are deliberately synchronous: all access is made from the owning
    asyncio event loop and no I/O happens here.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024):
        self.max_bytes = max(0, max_bytes)
        self._entries: OrderedDict[CacheId, CacheEntry] = OrderedDict()
        self._size_bytes = 0

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def get(self, cache_id: CacheId) -> CacheEntry | None:
        entry = self._entries.get(cache_id)
        if entry is not None:
            self._entries.move_to_end(cache_id)
        return entry

    def set(self, cache_id: CacheId, entry: CacheEntry) -> None:
        old = self._entries.pop(cache_id, None)
        if old is not None:
            self._size_bytes -= old.size_bytes
        if self.max_bytes == 0 or entry.size_bytes > self.max_bytes:
            return
        self._entries[cache_id] = entry
        self._size_bytes += entry.size_bytes
        while self._size_bytes > self.max_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._size_bytes -= evicted.size_bytes

    def delete(self, namespace: str, key_prefix: str | None = None) -> int:
        doomed = [
            cache_id for cache_id in self._entries
            if cache_id[0] == namespace
            and (key_prefix is None or cache_id[1].startswith(key_prefix))
        ]
        for cache_id in doomed:
            self._size_bytes -= self._entries.pop(cache_id).size_bytes
        return len(doomed)

    def delete_key(self, cache_id: CacheId) -> int:
        entry = self._entries.pop(cache_id, None)
        if entry is None:
            return 0
        self._size_bytes -= entry.size_bytes
        return 1

    def delete_namespace_prefix(self, namespace_prefix: str) -> int:
        doomed = [key for key in self._entries if key[0].startswith(namespace_prefix)]
        for cache_id in doomed:
            self._size_bytes -= self._entries.pop(cache_id).size_bytes
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()
        self._size_bytes = 0


class AsyncCache:
    """Byte-bounded LRU with stale serving and request coalescing."""

    def __init__(
        self,
        *,
        memory_max_bytes: int = 64 * 1024 * 1024,
    ):
        self.memory = MemoryCache(memory_max_bytes)
        self._inflight: dict[CacheId, asyncio.Task] = {}
        self._stats = {
            "memory_hits": 0,
            "misses": 0,
            "stale_hits": 0,
            "loads": 0,
            "load_errors": 0,
            "singleflight_waits": 0,
            "evictions_or_invalidations": 0,
        }

    @classmethod
    def _value_size(cls, value, seen: set[int] | None = None) -> int:
        """Estimate retained size without serializing/copying large strings."""
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        size = sys.getsizeof(value)
        if is_dataclass(value):
            return size + sum(cls._value_size(getattr(value, field.name), seen)
                              for field in fields(value))
        if isinstance(value, dict):
            return size + sum(cls._value_size(key, seen) + cls._value_size(item, seen)
                              for key, item in value.items())
        if isinstance(value, (list, tuple, set, frozenset)):
            return size + sum(cls._value_size(item, seen) for item in value)
        return size

    async def _lookup(self, cache_id: CacheId, policy: CachePolicy) -> CacheEntry | None:
        now = time.time()
        entry = self.memory.get(cache_id)
        if entry is not None:
            if entry.is_stale_usable(now):
                self._stats["memory_hits"] += 1
                return entry
            self.memory.delete_key(cache_id)
        return None

    async def get_or_load(
        self,
        namespace: str,
        key: str,
        policy: CachePolicy,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        cache_id = (namespace, key)
        entry = await self._lookup(cache_id, policy)
        now = time.time()
        if entry is not None and entry.is_fresh(now):
            return entry.value
        if entry is not None and entry.is_stale_usable(now):
            self._stats["stale_hits"] += 1
            if policy.stale_while_revalidate:
                self._start_load(cache_id, policy, loader)
                return entry.value
        self._stats["misses"] += 1
        task = self._inflight.get(cache_id)
        if task is None:
            task = self._start_load(cache_id, policy, loader)
        else:
            self._stats["singleflight_waits"] += 1
        return await asyncio.shield(task)

    def _start_load(
        self,
        cache_id: CacheId,
        policy: CachePolicy,
        loader: Callable[[], Awaitable[T]],
    ) -> asyncio.Task:
        existing = self._inflight.get(cache_id)
        if existing is not None:
            return existing
        task = asyncio.create_task(self._load_and_store(cache_id, policy, loader))
        self._inflight[cache_id] = task
        task.add_done_callback(lambda done: self._finish_load(cache_id, done))
        return task

    def _finish_load(self, cache_id: CacheId, task: asyncio.Task) -> None:
        if self._inflight.get(cache_id) is task:
            self._inflight.pop(cache_id, None)
        # Background refreshes have no waiter to retrieve their exception.
        if not task.cancelled():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass

    async def _load_and_store(
        self,
        cache_id: CacheId,
        policy: CachePolicy,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        self._stats["loads"] += 1
        try:
            value = await loader()
        except Exception:
            self._stats["load_errors"] += 1
            raise
        now = time.time()
        entry = CacheEntry(
            value=value,
            fresh_until=now + policy.fresh_ttl,
            stale_until=now + policy.fresh_ttl + policy.stale_ttl,
            size_bytes=self._value_size(value),
        )
        self.memory.set(cache_id, entry)
        return value

    async def invalidate(self, namespace: str, key_prefix: str | None = None) -> int:
        removed = self.memory.delete(namespace, key_prefix)
        self._stats["evictions_or_invalidations"] += removed
        return removed

    async def invalidate_key(self, namespace: str, key: str) -> int:
        removed = self.memory.delete_key((namespace, key))
        self._stats["evictions_or_invalidations"] += removed
        return removed

    async def invalidate_namespace_prefix(self, namespace_prefix: str) -> int:
        removed = self.memory.delete_namespace_prefix(namespace_prefix)
        self._stats["evictions_or_invalidations"] += removed
        return removed

    async def stats(self) -> dict:
        return {
            **self._stats,
            "memory_entries": self.memory.entry_count,
            "memory_size_bytes": self.memory.size_bytes,
            "inflight": len(self._inflight),
        }

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
