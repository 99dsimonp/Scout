from __future__ import annotations

import fcntl
from pathlib import Path
from typing import Optional, TextIO


class RuntimeLockError(RuntimeError):
    pass


class RuntimeLock:
    def __init__(self, state_dir: str, name: str = "scout.lock"):
        self.path = Path(state_dir) / name
        self._file: Optional[TextIO] = None

    def __enter__(self) -> "RuntimeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RuntimeLockError(
                "another Scout process is already using {}".format(self.path)
            ) from exc
        self._file = lock_file
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
