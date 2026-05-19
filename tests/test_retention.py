import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scout.retention import cleanup_review_artifacts


class RetentionTests(unittest.TestCase):
    def test_cleanup_removes_old_run_dirs_and_keeps_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            old_run = runs / "old"
            recent_run = runs / "recent"
            old_run.mkdir(parents=True)
            recent_run.mkdir()
            now = datetime(2026, 5, 15, tzinfo=timezone.utc)
            old_ts = (now - timedelta(days=8)).timestamp()
            recent_ts = (now - timedelta(days=1)).timestamp()
            os.utime(old_run, (old_ts, old_ts))
            os.utime(recent_run, (recent_ts, recent_ts))

            cleanup_review_artifacts(tmp, retention_days=7, now=now)

            self.assertFalse(old_run.exists())
            self.assertTrue(recent_run.exists())

    def test_cleanup_rewrites_review_log_to_retention_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-log.jsonl"
            now = datetime(2026, 5, 15, tzinfo=timezone.utc)
            old = {"timestamp": (now - timedelta(days=8)).isoformat(), "provider": "codex"}
            recent = {"timestamp": (now - timedelta(days=2)).isoformat(), "provider": "claude"}
            path.write_text(
                json.dumps(old) + "\n" + json.dumps(recent) + "\nnot-json\n",
                encoding="utf-8",
            )

            cleanup_review_artifacts(tmp, retention_days=7, now=now)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), recent)


if __name__ == "__main__":
    unittest.main()
