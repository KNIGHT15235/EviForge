from __future__ import annotations

import threading

FileVersion = tuple[int, int, int, int]


class FileCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._versions: dict[str, FileVersion | None] = {}
        self._lock = threading.Lock()

    def get(self, path: str, *, version: FileVersion | None = None) -> str | None:
        with self._lock:
            if version is not None and self._versions.get(path) != version:
                self._store.pop(path, None)
                self._versions.pop(path, None)
                return None
            return self._store.get(path)


    def put(self, path: str, content: str, *, version: FileVersion | None = None) -> None:
        with self._lock:
            self._store[path] = content
            self._versions[path] = version


    def invalidate(self, path: str) -> None:
        with self._lock:
            self._store.pop(path, None)
            self._versions.pop(path, None)


    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._versions.clear()


    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
