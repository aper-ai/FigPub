"""A byte-bounded preview cache with per-key concurrent request coalescing."""
from collections import OrderedDict
from concurrent.futures import Future
from threading import BoundedSemaphore, RLock


class PreviewBusyError(RuntimeError):
    pass


class PreviewCache:
    def __init__(self, max_bytes=48 * 1024 * 1024, max_entries=48, max_renderers=2, max_pending=8):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._items, self._pending = OrderedDict(), {}
        self._bytes, self._lock = 0, RLock()
        self._render_slots, self._max_pending = BoundedSemaphore(max_renderers), max_pending

    def clear(self):
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def get_or_create(self, key, build):
        with self._lock:
            if key in self._items:
                value = self._items.pop(key)
                self._items[key] = value
                return value
            pending = self._pending.get(key)
            owner = pending is None
            if owner:
                if len(self._pending) >= self._max_pending:
                    raise PreviewBusyError('预览正在更新，请稍候；已保留当前原始数据和设置。')
                pending = self._pending[key] = Future()
        if not owner:
            return pending.result()
        try:
            with self._render_slots:
                value = build()
            with self._lock:
                if len(value) <= self.max_bytes:
                    while self._items and (self._bytes + len(value) > self.max_bytes or len(self._items) >= self.max_entries):
                        self._bytes -= len(self._items.popitem(last=False)[1])
                    self._items[key] = value
                    self._bytes += len(value)
                self._pending.pop(key, None)
                pending.set_result(value)
            return value
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key, None)
                pending.set_exception(exc)
            raise
