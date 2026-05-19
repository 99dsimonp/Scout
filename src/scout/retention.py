from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)


def cleanup_review_artifacts(
    state_dir: str,
    retention_days: int,
    now: Optional[datetime] = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current - timedelta(days=retention_days)
    _cleanup_runs(Path(state_dir) / "runs", cutoff)
    _cleanup_review_log(Path(state_dir) / "review-log.jsonl", cutoff)
    _cleanup_review_log(Path(state_dir) / "provider-usage.jsonl", cutoff)


def _cleanup_runs(runs_dir: Path, cutoff: datetime) -> None:
    if not runs_dir.exists():
        return
    cutoff_timestamp = cutoff.timestamp()
    for path in runs_dir.iterdir():
        try:
            if path.stat().st_mtime >= cutoff_timestamp:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            LOG.warning("failed to remove expired review artifact path=%s error=%s", path, exc)


def _cleanup_review_log(path: Path, cutoff: datetime) -> None:
    if not path.exists():
        return

    kept = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        LOG.warning("failed to read review log for retention path=%s error=%s", path, exc)
        return

    for line in lines:
        timestamp = _line_timestamp(line)
        if timestamp is not None and timestamp >= cutoff:
            kept.append(line)

    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        LOG.warning("failed to rewrite review log for retention path=%s error=%s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def _line_timestamp(line: str) -> Optional[datetime]:
    try:
        value = json.loads(line).get("timestamp")
    except (AttributeError, json.JSONDecodeError):
        return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
