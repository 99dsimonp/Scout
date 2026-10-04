import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scout.cli import main
from scout.daemon import ScoutDaemon, _append_review_log_entry
from scout.provider import ProviderSuperseded, run_provider_command
from scout.retention import cleanup_review_artifacts
from scout.state import StateStore


class DiagnosticDatabaseTests(unittest.TestCase):
    def test_idle_connection_keeps_sidecars_without_blocking_writes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": str(os.getuid())}):
            path = Path(tmp) / "state.db"
            store = StateStore(str(path))
            store.initialize()
            store.open_diagnostic_reader()
            self.addCleanup(store.close_diagnostic_reader)
            self.assertFalse(store._diagnostic_connection.in_transaction)
            self.assertTrue(Path(str(path) + "-wal").exists())
            self.assertTrue(Path(str(path) + "-shm").exists())
            errors = []

            def write():
                try:
                    store.upsert_repository("ws", "repo", "ssh://repo")
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=write)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "idle diagnostics connection blocked a writer")
            self.assertEqual(errors, [])
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as reader:
                reader.execute("pragma query_only=ON")
                self.assertEqual(reader.execute("select repo_slug from repositories").fetchone()[0], "repo")
            reader.close()
            store.close_diagnostic_reader()
            self.assertIsNone(store._diagnostic_connection)
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_disabled_diagnostics_does_not_open_connection_or_require_acl_tool(self):
        with patch.dict(os.environ, {}, clear=True), patch("scout.state.sqlite3.connect") as connect:
            store = StateStore("missing.db")
            store.open_diagnostic_reader()
            store.close_diagnostic_reader()
            connect.assert_not_called()

    def test_unavailable_diagnostic_connection_does_not_stop_scout(self):
        with patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": "scout-mcp"}), patch(
            "scout.state.sqlite3.connect", side_effect=sqlite3.OperationalError("readonly database")
        ):
            store = StateStore("missing.db")
            with self.assertLogs("scout.state", level="WARNING"):
                store.open_diagnostic_reader()
            self.assertIsNone(store._diagnostic_connection)

    def test_run_methods_close_connection_on_initialization_failure_and_interruption(self):
        for method in ("run_once", "run_forever"):
            for error in (RuntimeError("startup failed"), KeyboardInterrupt()):
                with self.subTest(method=method, error=type(error)):
                    daemon = ScoutDaemon.__new__(ScoutDaemon)
                    daemon.config = Mock()
                    daemon.state = Mock()
                    daemon.initialize = Mock(side_effect=error)
                    with patch("scout.daemon.RuntimeLock"):
                        with self.assertRaises(type(error)):
                            getattr(daemon, method)()
                    daemon.state.close_diagnostic_reader.assert_called_once_with()

    def test_once_closes_real_idle_connection_after_poll_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": str(os.getuid())}):
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = Mock()
            daemon.config.service.state_dir = tmp
            daemon.state = StateStore(str(Path(tmp) / "state.db"))
            daemon.state.initialize()
            daemon.initialize = daemon.state.open_diagnostic_reader
            daemon.poll_once = Mock(side_effect=RuntimeError("poll failed"))
            with self.assertRaisesRegex(RuntimeError, "poll failed"):
                daemon.run_once()
            self.assertIsNone(daemon.state._diagnostic_connection)
            self.assertFalse(Path(tmp, "state.db-wal").exists())

    def test_schema_read_failure_closes_partial_connection(self):
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("corrupt database")
        with patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": "scout-mcp"}), patch(
            "scout.state.sqlite3.connect", return_value=connection
        ), self.assertLogs("scout.state", level="WARNING"):
            store = StateStore("missing.db")
            store.open_diagnostic_reader()
        connection.close.assert_called_once_with()
        self.assertIsNone(store._diagnostic_connection)


class DiagnosticLoggingTests(unittest.TestCase):
    def test_logging_refuses_symlink_leaf_and_keeps_secret_unchanged(self):
        from scout.diagnostic_logging import configure_diagnostic_logging

        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "provider-auth.json"
            secret.write_text('{"token":"keep unchanged"}')
            log = Path(tmp) / "scout.log"
            log.symlink_to(secret)
            with patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_LOG": str(log)}, clear=True):
                handler = configure_diagnostic_logging()
            if handler is not None:
                self.addCleanup(logging.getLogger().removeHandler, handler)
                self.addCleanup(handler.close)
                handler.emit(logging.LogRecord("test", logging.ERROR, __file__, 1, "must not append", (), None))
            self.assertIsNone(handler)
            self.assertEqual(secret.read_text(), '{"token":"keep unchanged"}')

    def test_logging_refuses_symlink_parent(self):
        from scout.diagnostic_logging import configure_diagnostic_logging

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "provider-home"
            target.mkdir()
            parent = Path(tmp) / "diagnostics"
            parent.symlink_to(target, target_is_directory=True)
            with patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_LOG": str(parent / "scout.log")}, clear=True):
                handler = configure_diagnostic_logging()
            if handler is not None:
                self.addCleanup(logging.getLogger().removeHandler, handler)
                self.addCleanup(handler.close)
            self.assertIsNone(handler)
            self.assertFalse((target / "scout.log").exists())

    def test_symlink_on_rotation_reopen_does_not_escape_or_change_target(self):
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "scout.log"
            handler = DiagnosticLogHandler(log, 7)
            self.addCleanup(handler.close)
            secret = Path(tmp) / "provider-auth.json"
            secret.write_text("secret")
            original_rotate = handler.rotate

            def rotate(source, destination):
                original_rotate(source, destination)
                log.symlink_to(secret)

            with patch.object(handler, "shouldRollover", return_value=True), patch.object(handler, "rotate", side_effect=rotate), redirect_stderr(io.StringIO()):
                handler.emit(logging.LogRecord("test", logging.ERROR, __file__, 1, "must not append", (), None))
            self.assertEqual(secret.read_text(), "secret")

    def test_configuration_failure_is_logged_before_reraising(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_LOG": str(Path(tmp) / "diagnostics" / "scout.log")}, clear=True):
            with patch("scout.cli.load_config", side_effect=ValueError("configuration is broken")):
                with self.assertRaisesRegex(ValueError, "configuration is broken"):
                    main(["--check-config"])
            self.assertIn("configuration is broken", (Path(tmp) / "diagnostics" / "scout.log").read_text())

    def test_logging_write_and_rollover_failures_do_not_escape(self):
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp:
            handler = DiagnosticLogHandler(Path(tmp) / "scout.log", 7)
            self.addCleanup(handler.close)
            record = logging.LogRecord("test", logging.ERROR, __file__, 1, "message", (), None)
            with patch.object(handler, "shouldRollover", return_value=True), patch.object(handler, "doRollover", side_effect=OSError("read-only filesystem")), redirect_stderr(io.StringIO()):
                handler.emit(record)
            with patch.object(handler.stream, "write", side_effect=OSError("disk full")), redirect_stderr(io.StringIO()):
                handler.emit(record)
            with patch.object(handler, "flush", side_effect=OSError("disk full")), redirect_stderr(io.StringIO()):
                handler.close()

    def test_logging_setup_failure_preserves_command_behavior(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_LOG": tmp}, clear=True):
            config = Mock()
            config.service.log_level = "INFO"
            with patch("scout.cli.load_config", return_value=config), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--check-config"]), 0)

    def test_expired_rotations_are_pruned_after_downtime(self):
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scout.log"
            old = Path(tmp) / "scout.log.2020-01-01"
            old.write_text("expired")
            expired = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
            os.utime(old, (expired, expired))
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            recent = Path(tmp) / ("scout.log." + yesterday.strftime("%Y-%m-%d"))
            recent.write_text("recent")
            other = Path(tmp) / "unrelated.log.2020-01-01"
            other.write_text("unrelated")
            handler = DiagnosticLogHandler(path, 7)
            self.addCleanup(handler.close)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(other.exists())

    def test_one_day_retention_does_not_keep_records_from_an_older_daily_interval(self):
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp:
            # The old rotation has a recent mtime; it still contains records
            # from more than a day ago and must expire by its interval start.
            old = Path(tmp) / "scout.log.2026-05-01"
            old.write_text("old day")
            now = datetime(2026, 5, 2, 12, tzinfo=timezone.utc).timestamp()
            with patch("scout.diagnostic_logging.time.time", return_value=now):
                handler = DiagnosticLogHandler(Path(tmp) / "scout.log", 1)
            self.addCleanup(handler.close)
            self.assertFalse(old.exists())
            self.assertEqual(handler.backupCount, 1)

    def test_unreadable_log_retention_does_not_disable_logging(self):
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            with patch.object(DiagnosticLogHandler, "_prune_expired", side_effect=OSError("read-only filesystem")):
                handler = DiagnosticLogHandler(Path(tmp) / "scout.log", 7)
            self.addCleanup(handler.close)
            handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "alive", (), None))
            self.assertIn("alive", (Path(tmp) / "scout.log").read_text())


@unittest.skipUnless(shutil.which("setfacl") and shutil.which("getfacl"), "POSIX ACL tools unavailable")
class DiagnosticAccessTests(unittest.TestCase):
    # The named entry is for an unused UID, so effective rights cannot be supplied
    # accidentally by the file owner's entry when these tests use a strict umask.
    reader = "61123"

    def acl(self, path):
        return subprocess.check_output(["getfacl", "-cpn", str(path)], text=True)

    def test_adding_reader_preserves_all_existing_effective_access_permissions(self):
        from scout.diagnostic_access import grant_diagnostic_file, grant_diagnostic_traversal

        for directory, grant in ((True, grant_diagnostic_traversal), (False, grant_diagnostic_file)):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": self.reader}):
                path = Path(tmp) / "shared"
                path.mkdir() if directory else path.touch()
                subprocess.run(["setfacl", "-m", "u:61124:rwx,g::rwx,g:61125:rw-,m::--x", str(path)], check=True)
                grant(path)
                acl = self.acl(path)
                self.assertIn("user:61124:--x", acl)
                self.assertIn("group::--x", acl)
                self.assertIn("group:61125:---", acl)
                self.assertIn("user:{}:{}".format(self.reader, "--x" if directory else "r--"), acl)
                self.assertNotIn("#effective:", acl)

    def test_inherited_reader_does_not_unmask_existing_default_permissions(self):
        from scout.diagnostic_access import prepare_diagnostic_directory

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": self.reader}):
            runs = Path(tmp) / "runs"
            runs.mkdir()
            subprocess.run(["setfacl", "-m", "d:u:61124:rwx,d:g::rwx,d:g:61125:rw-,d:m::--x", str(runs)], check=True)
            prepare_diagnostic_directory(runs)
            acl = self.acl(runs)
            self.assertIn("default:user:61124:--x", acl)
            self.assertIn("default:group::--x", acl)
            self.assertIn("default:group:61125:---", acl)
            self.assertIn("default:user:{}:r-x".format(self.reader), acl)

    def test_strict_umask_log_rotation_and_retention_keep_reader_access(self):
        from scout.diagnostic_access import prepare_diagnostic_directory
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": self.reader}):
            old_umask = os.umask(0o077)
            self.addCleanup(os.umask, old_umask)
            root = Path(tmp)
            prepare_diagnostic_directory(root / "runs")
            self.assertIn("default:user:{}:r-x".format(self.reader), self.acl(root / "runs"))
            self.assertNotIn("default:", self.acl(root))
            now = datetime.now(timezone.utc)
            path = _append_review_log_entry(tmp, {"timestamp": now.isoformat(), "message": "recent"})
            _append_review_log_entry(tmp, {"timestamp": (now - timedelta(days=8)).isoformat()})
            self.assertIn("user:{}:r--".format(self.reader), self.acl(path))
            cleanup_review_artifacts(tmp, 7, now=now)
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertIn("user:{}:r--".format(self.reader), self.acl(path))
            handler = DiagnosticLogHandler(root / "diagnostics" / "scout.log", 7)
            self.addCleanup(handler.close)
            handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "before", (), None))
            handler.doRollover()
            handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "after", (), None))
            for log in (root / "diagnostics").iterdir():
                self.assertIn("user:{}:r--".format(self.reader), self.acl(log))

    def test_acl_errors_are_nonfatal_and_symlinks_are_not_granted_access(self):
        from scout.diagnostic_access import grant_diagnostic_file

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": self.reader}):
            target = Path(tmp) / "secret"
            target.write_text("secret")
            link = Path(tmp) / "link"
            link.symlink_to(target)
            with self.assertLogs("scout.diagnostic_access", level="WARNING"):
                grant_diagnostic_file(link)
            self.assertNotIn("user:{}:".format(self.reader), self.acl(target))
            for error in (OSError("read-only filesystem"), subprocess.TimeoutExpired("setfacl", 1)):
                with patch("scout.diagnostic_access.subprocess.run", side_effect=error), self.assertLogs("scout.diagnostic_access", level="WARNING"):
                    grant_diagnostic_file(target)

    def test_provider_restores_restrictive_output_acl_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": self.reader}):
            run = Path(tmp)
            prompt = run / "prompt.txt"
            prompt.write_text("prompt")
            final = run / "codex-final-message.json"
            script = "import os,time; fd=os.open({!r},os.O_CREAT|os.O_WRONLY,0o600); os.write(fd,b'partial'); os.close(fd); time.sleep(30)".format(str(final))
            with patch("scout.provider.POLL_INTERVAL_SECONDS", 0.01):
                with self.assertRaises(ProviderSuperseded):
                    run_provider_command(
                        "codex", "test", [sys.executable, "-c", script], prompt,
                        run / "codex-stdout.log", run / "codex-stderr.log", {}, 5,
                        final.exists, "superseded", output_file=final,
                    )
            self.assertIn("user:{}:r--".format(self.reader), self.acl(final))

    def test_existing_jsonl_append_does_not_run_permission_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            _append_review_log_entry(tmp, {"message": "first"})
            with patch("scout.daemon.grant_diagnostic_file") as grant:
                _append_review_log_entry(tmp, {"message": "second"})
            grant.assert_not_called()

    @unittest.skipUnless(os.geteuid() == 0, "different-UID access test requires root, as in the Docker validation target")
    def test_restricted_reader_reads_database_logs_and_runs_but_not_credentials(self):
        from scout.diagnostic_access import grant_diagnostic_file, prepare_diagnostic_directory
        from scout.diagnostic_logging import DiagnosticLogHandler

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SCOUT_DIAGNOSTIC_READER": self.reader}):
            root = Path(tmp)
            old_umask = os.umask(0o077)
            self.addCleanup(os.umask, old_umask)
            store = StateStore(str(root / "state.db"))
            store.initialize()
            store.open_diagnostic_reader()
            self.addCleanup(store.close_diagnostic_reader)
            store.upsert_repository("ws", "repo", "ssh://repo")
            secret = root / "credentials.env"
            secret.write_text("secret")
            prepare_diagnostic_directory(root / "runs")
            run = root / "runs" / "1"
            run.mkdir()
            artifact = run / "codex-final-message.json"
            artifact.write_text("output")
            artifact.chmod(0o600)
            grant_diagnostic_file(artifact)
            handler = DiagnosticLogHandler(root / "diagnostics" / "scout.log", 7)
            self.addCleanup(handler.close)
            handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "diagnostic", (), None))
            _append_review_log_entry(tmp, {"timestamp": datetime.now(timezone.utc).isoformat()})
            cleanup_review_artifacts(tmp, 7)
            script = """
import json, pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
connection = sqlite3.connect((root / 'state.db').as_uri() + '?mode=ro', uri=True)
connection.execute('pragma query_only=ON')
assert connection.execute('select repo_slug from repositories').fetchone()[0] == 'repo'
try:
    connection.execute('delete from repositories')
except sqlite3.OperationalError:
    pass
else:
    raise AssertionError('reader wrote to database')
connection.close()
assert (root / 'runs/1/codex-final-message.json').read_text() == 'output'
assert 'diagnostic' in (root / 'diagnostics/scout.log').read_text()
assert json.loads((root / 'review-log.jsonl').read_text())['timestamp']
try:
    (root / 'credentials.env').read_text()
except PermissionError:
    pass
else:
    raise AssertionError('reader accessed credentials')
print(json.dumps({'read_only': True}))
"""
            result = subprocess.run(
                [sys.executable, "-c", script, tmp], user=int(self.reader), group=int(self.reader),
                extra_groups=(), capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["read_only"])


if __name__ == "__main__":
    unittest.main()
