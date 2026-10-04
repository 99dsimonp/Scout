"""Optional diagnostic file logging; stderr remains the primary log sink."""
from __future__ import annotations

import logging
import os
import stat
import sys
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from .diagnostic_access import grant_diagnostic_file, open_diagnostic_path, prepare_diagnostic_directory

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class DiagnosticLogHandler(TimedRotatingFileHandler):
    def __init__(self, path: Path, retention_days: int):
        self.retention_days = min(7, max(1, retention_days))
        self._failure_reported = False
        path.parent.mkdir(parents=True, exist_ok=True)
        prepare_diagnostic_directory(path.parent)
        super().__init__(
            str(path), when="midnight", backupCount=self.retention_days,
            encoding="utf-8", utc=True,
        )
        self.addFilter(lambda record: not getattr(record, "diagnostic_internal", False))
        self.setFormatter(logging.Formatter(LOG_FORMAT))
        try:
            self._prune_expired()
        except OSError:
            self.handleError(None)

    def _open(self):
        path = Path(self.baseFilename)
        fd = open_diagnostic_path(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("diagnostic log must be a regular file")
            stream = os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)
            fd = None
            grant_diagnostic_file(path)
            return stream
        finally:
            if fd is not None:
                os.close(fd)

    def getFilesToDelete(self):
        expired = set(super().getFilesToDelete())
        cutoff = time.time() - self.retention_days * 86400
        path = Path(self.baseFilename)
        for candidate in path.parent.glob(path.name + ".*"):
            suffix = candidate.name[len(path.name) + 1:]
            if self.extMatch.fullmatch(suffix):
                try:
                    started = datetime.strptime(suffix, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                # A day's final write can be nearly 24 hours after its first
                # record. Use the interval start, not mtime, to cap record age.
                if started.timestamp() < cutoff:
                    expired.add(str(candidate))
        return sorted(expired)

    def _prune_expired(self) -> None:
        for filename in self.getFilesToDelete():
            Path(filename).unlink(missing_ok=True)

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        if self._failure_reported:
            return
        self._failure_reported = True
        try:
            sys.stderr.write("Scout diagnostic file logging unavailable; continuing with stderr logging\n")
        except Exception:
            pass

    def close(self):
        try:
            super().close()
        except Exception:
            self.handleError(None)


def configure_diagnostic_logging() -> Optional[DiagnosticLogHandler]:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    filename = os.environ.get("SCOUT_DIAGNOSTIC_LOG", "")
    if not filename:
        return None
    try:
        retention_days = int(os.environ.get("SCOUT_DIAGNOSTIC_RETENTION_DAYS", "7"))
        handler = DiagnosticLogHandler(Path(filename), retention_days)
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("cannot initialize diagnostic logging: %s", exc)
        return None
    logging.getLogger().addHandler(handler)
    return handler
