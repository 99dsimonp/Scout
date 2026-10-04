import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from scout.diagnostics import Diagnostics
from scout.diagnostic_files import redact
from scout.models import PullRequest
from scout.state import StateStore
from scout.usage import summarize_usage_log


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = SimpleNamespace(
            state_dir=str(self.root), state_db=str(self.root / "state.db"),
            log_path=str(self.root / "daemon.log"), max_records=200,
            max_bytes=65536, max_scan_bytes=8388608, query_timeout_seconds=5,
        )
        self.store = StateStore(self.config.state_db)
        self.store.initialize()
        self.reader = Diagnostics(self.config)
        self.service = patch("scout.diagnostics.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout="ActiveState=active\nSubState=running\nEnvironment=SECRET\n"))
        self.service.start()
        self.addCleanup(self.service.stop)

    def job(self, number=1, provider="codex"):
        pr = PullRequest("ws", "repo", number, "Review", "", "feature", "a" * 40, "main")
        self.store.enqueue_or_update_pr(pr, "v1", "v1", provider)
        with self.store.connect() as conn:
            return conn.execute("select id from review_jobs where pr_id=?", (number,)).fetchone()[0]

    def write_log(self, source, rows):
        name = {"usage": "provider-usage.jsonl", "review": "review-log.jsonl"}[source]
        path = self.root / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def snapshot(self):
        with self.store.connect() as conn:
            return "\n".join(conn.iterdump())

    def test_reads_never_change_state_or_clear_expired_cooldowns(self):
        job = self.job()
        self.store.mark_provider_cooldown("codex", "token=abc-secret", -1)
        before = self.snapshot()
        with patch.object(StateStore, "initialize", side_effect=AssertionError("migration")):
            status = self.reader.get_status()
            self.assertEqual(status["database"]["status"], "ok")
            self.assertFalse(status["database"]["providers"][0]["cooldown_active"])
            self.assertNotIn("Environment", status["service"])
            self.assertEqual(self.reader.list_jobs()["jobs"][0]["id"], job)
            self.assertEqual(self.reader.get_job(job)["job"]["id"], job)
        self.assertEqual(before, self.snapshot())
        with self.reader._connect() as conn:
            self.assertEqual(conn.execute("pragma query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("update review_jobs set status='cancelled'")

    def test_missing_and_incompatible_database_leave_logs_available(self):
        Path(self.config.log_path).write_text("still running\n")
        Path(self.config.state_db).unlink()
        result = self.reader.get_status()
        self.assertEqual(result["database"]["status"], "unavailable")
        self.assertEqual(result["sources"]["daemon"]["status"], "ok")
        self.assertFalse(Path(self.config.state_db).exists())
        sqlite3.connect(self.config.state_db).close()
        self.assertEqual(self.reader.list_jobs()["status"], "unavailable")
        self.assertIn("still running", str(self.reader.read_logs()))

    def test_job_pages_and_filters_exclude_lease_and_configuration(self):
        for number in range(1, 6):
            self.job(number)
        with self.store.connect() as conn:
            conn.execute("update review_jobs set lease_token='secret',error_message='Authorization: Bearer eyJ.secret.signature'")
        page = self.reader.list_jobs(repository="repo", provider="codex", limit=2)
        self.assertEqual([j["pr_id"] for j in page["jobs"]], [1, 2])
        next_page = self.reader.list_jobs(repository="repo", provider="codex", limit=2, cursor=page["next_cursor"])
        self.assertEqual([j["pr_id"] for j in next_page["jobs"]], [3, 4])
        self.assertNotIn("lease_token", json.dumps(page))
        self.assertNotIn("eyJ.secret.signature", json.dumps(page))
        self.assertEqual(self.reader.list_jobs(repository="repo' OR 1=1--")["jobs"], [])
        self.assertEqual(self.reader.list_jobs(cursor="../../secret")["status"], "invalid_request")

    def test_database_reader_does_not_block_concurrent_writer(self):
        job = self.job()
        errors = []
        with self.store.connect() as keeper:
            keeper.execute("select 1")
            def write():
                try:
                    for attempt in range(20):
                        with self.store.connect() as conn:
                            conn.execute("update review_jobs set attempts=? where id=?", (attempt, job))
                except Exception as exc:
                    errors.append(exc)
            writer = threading.Thread(target=write)
            writer.start()
            for _ in range(20):
                self.assertEqual(self.reader.get_job(job)["status"], "ok")
            writer.join(timeout=5)
            self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])

    def test_job_details_expose_publication_blockers_without_raw_snapshots(self):
        pr = PullRequest("ws", "repo", 1, "Review", "", "feature", "a" * 40, "main")
        round_ = self.store.inline.create_round(pr, ["codex"], "v1", "v1")
        job = self.reader.list_jobs()["jobs"][0]["id"]
        with self.store.connect() as conn:
            conn.execute("update inline_rounds set status='publication_failed',error='token=very-secret'")
            conn.execute("insert into inline_publication_intents(id,round_id,intent_key,kind,marker,payload,status,created_at,updated_at) values('i',?,'k','finding','marker','secret-payload','unknown','now','now')", (round_["id"],))
        result = self.reader.get_job(job)
        self.assertEqual(result["rounds"][0]["status"], "publication_failed")
        self.assertEqual(result["publication_intents"][0]["status"], "unknown")
        self.assertNotIn("secret-payload", json.dumps(result))
        self.assertNotIn("very-secret", json.dumps(result))
        self.assertNotIn("snapshot", json.dumps(result))

    def test_daemon_logs_offer_bounded_retained_dates_and_read_selected_rotation(self):
        today = datetime.now(timezone.utc).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        oldest = (today - timedelta(days=7)).isoformat()
        expired = (today - timedelta(days=8)).isoformat()
        active = Path(self.config.log_path)
        active.write_text("current daemon log\n")
        Path(str(active) + "." + yesterday).write_text("yesterday daemon log\n")
        Path(str(active) + "." + oldest).write_text("oldest retained daemon log\n")
        Path(str(active) + "." + expired).write_text("expired daemon log\n")
        current = self.reader.read_logs()
        self.assertEqual(current["records"], ["current daemon log"])
        self.assertEqual(current["available_rotations"], [yesterday, oldest])
        retained = self.reader.read_logs(rotation=yesterday)
        self.assertEqual(retained["records"], ["yesterday daemon log"])
        self.assertEqual(retained["rotation"], yesterday)
        self.assertEqual(self.reader.read_logs(rotation=yesterday, cursor=current["next_cursor"])["status"], "invalid_request")
        self.assertEqual(self.reader.read_logs(rotation=expired)["status"], "invalid_request")
        self.assertEqual(self.reader.read_logs(rotation="../../secret")["status"], "invalid_request")
        self.assertEqual(self.reader.read_logs(rotation="2026-02-30")["status"], "invalid_request")
        self.assertEqual(self.reader.read_logs("usage", rotation=yesterday)["status"], "invalid_request")

    def test_rotated_daemon_log_symlink_is_neither_listed_nor_read(self):
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        active = Path(self.config.log_path)
        active.write_text("current daemon log\n")
        secret = self.root / "secret"
        secret.write_text("secret file")
        Path(str(active) + "." + yesterday).symlink_to(secret)
        self.assertEqual(self.reader.read_logs()["available_rotations"], [])
        self.assertEqual(self.reader.read_logs(rotation=yesterday)["status"], "unavailable")

    def test_partial_lines_and_rotation_have_explicit_continuation(self):
        path = self.write_log("review", [{"provider": "codex", "pr": 1}])
        with path.open("a") as handle:
            handle.write("{\"provider\":\"claude\"")
        result = self.reader.read_logs("review")
        self.assertEqual(len(result["records"]), 1)
        self.assertTrue(result["partial_line"])
        with path.open("a") as handle:
            handle.write(",\"pr\":2}\n")
        later = self.reader.read_logs("review", cursor=result["next_cursor"])
        self.assertEqual(later["records"][0]["pr"], 2)
        old_cursor = later["next_cursor"]
        replacement = path.with_suffix(".tmp")
        replacement.write_text("{\"pr\":3}\n")
        os.replace(replacement, path)
        self.assertEqual(self.reader.read_logs("review", cursor=old_cursor)["status"], "stale_cursor")

    def test_jsonl_allowlist_redaction_and_bounded_huge_records(self):
        self.config.max_bytes = 2048
        self.reader = Diagnostics(self.config)
        self.write_log("usage", [
            {"repo": "repo", "pr": 1, "error": "password=super-secret", "raw_provider_logs": {"stdout": "/etc/shadow"}, "configuration": "secret"},
            {"repo": "repo", "error": "x" * 100000},
            {"repo": "repo", "pr": 2},
        ])
        result = self.reader.read_logs("usage")
        encoded = json.dumps(result, ensure_ascii=False).encode()
        self.assertLessEqual(len(encoded), 2048)
        self.assertNotIn(b"super-secret", encoded)
        self.assertNotIn(b"/etc/shadow", encoded)
        self.assertNotIn(b"configuration", encoded)
        self.assertGreaterEqual(result["omitted_records"], 1)

    def test_artifacts_use_owned_paths_and_reject_symlinks(self):
        job = self.job()
        run = self.root / "runs" / str(job)
        run.mkdir(parents=True)
        secret = self.root / "secret"
        secret.write_text("sensitive")
        artifact = run / "codex-stdout.log"
        artifact.symlink_to(secret)
        self.assertEqual(self.reader.read_run_output(job)["status"], "unavailable")
        artifact.unlink()
        artifact.write_text("result token=secret-token\n")
        result = self.reader.read_run_output(job)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("secret-token", result["content"])
        self.assertIn("latest retained", result["note"])
        self.assertEqual(self.reader.read_run_output(job, artifact="../../secret")["status"], "invalid_request")
        artifact.unlink()
        run.rmdir()
        run.symlink_to(self.root)
        (self.root / "codex-stdout.log").write_text("sensitive")
        self.assertEqual(self.reader.read_run_output(job)["status"], "unavailable")

    def test_multiline_private_key_is_redacted_across_pages(self):
        path = Path(self.config.log_path)
        path.write_text("-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n-----END PRIVATE KEY-----\nordinary\n")
        result = self.reader.read_logs(limit=1)
        following = self.reader.read_logs(cursor=result["next_cursor"])
        self.assertNotIn("A" * 64, json.dumps([result, following]))
        self.assertIn("ordinary", json.dumps(following))

    def test_usage_matches_existing_totals_with_filters_and_page_subtotals(self):
        rows = [
            {"timestamp": "2026-10-01T00:00:00+00:00", "workspace": "ws", "repo": "repo", "pr": 1, "provider": "codex", "commit": "a", "usage": {"total_tokens": 10}},
            {"timestamp": "2026-10-02T00:00:00+00:00", "workspace": "ws", "repo": "repo", "pr": 1, "provider": "codex", "commit": "b", "usage": {"total_tokens": 20}},
            {"timestamp": "2026-10-02T00:00:00+00:00", "workspace": "ws", "repo": "repo", "pr": 2, "provider": "claude", "usage": {"total_tokens": 3, "cost_usd": 0.1}},
        ]
        path = self.write_log("usage", rows)
        result = self.reader.get_usage()
        self.assertEqual(result["usage"], summarize_usage_log(path))
        filtered = self.reader.get_usage(repository="repo", pr_id=1, since="2026-10-02T00:00:00Z")
        self.assertEqual(filtered["usage"][0]["total_tokens"], 20)
        self.config.max_records = 1
        self.reader = Diagnostics(self.config)
        first = self.reader.get_usage()
        second = self.reader.get_usage(cursor=first["next_cursor"])
        self.assertEqual(first["usage"][0]["total_tokens"] + second["usage"][0]["total_tokens"], 30)
        self.assertTrue(first["truncated"])

    def test_redaction_of_long_nonsecret_prefixes_and_urls_is_bounded(self):
        # These inputs fit one permitted log record. A failed match must not
        # restart at every hyphen or retry every possible URL colon split.
        for text in ("a-" * 11000 + "ordinary", "--" + "a-" * 11000 + "ordinary",
                     "https://" + "a:" * 11000 + "ordinary"):
            with self.subTest(prefix=text[:10]):
                started = time.perf_counter()
                result = redact(text)
                elapsed = time.perf_counter() - started
                self.assertEqual(result, text)
                self.assertLess(elapsed, 1.0)
        self.assertNotIn("very-sensitive", redact("a-" * 11000 + "token=very-sensitive"))
        self.assertNotIn("very-sensitive", redact("--" + "a-" * 11000 + "token=very-sensitive"))
        self.assertEqual(redact("https://user:very-sensitive@example.com/repo"), "https://[REDACTED]@example.com/repo")

    def test_redaction_preserves_cli_option_names_without_exposing_values(self):
        for text, expected in (
            ("--token=very-sensitive", "--token=[REDACTED]"),
            ("--api-key=\"very-sensitive\"", "--api-key=[REDACTED]"),
            ("command --password=\"correct horse battery staple\"", "command --password=[REDACTED]"),
            ("-token=very-sensitive", "-token=[REDACTED]"),
        ):
            with self.subTest(option=text.split("=", 1)[0]):
                self.assertEqual(redact(text), expected)

    def test_redaction_covers_quoted_authorization_and_password_values(self):
        self.assertNotIn("very-sensitive-token", redact("{\"Authorization\": \"Bearer very-sensitive-token\"}"))
        self.assertNotIn("correct horse battery staple", redact("password=\"correct horse battery staple\""))
        self.assertNotIn("horse", redact("password=\"correct horse battery staple\""))
        self.assertNotIn("env-secret", redact("SCOUT_BITBUCKET_TOKEN=env-secret"))

    def test_private_key_header_split_at_scan_limit_does_not_expose_body(self):
        self.config.max_scan_bytes = 100
        self.reader = Diagnostics(self.config)
        Path(self.config.log_path).write_text("x" * 83 + "\n-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n-----END PRIVATE KEY-----\nordinary\n")
        results, cursor = [], None
        for _ in range(10):
            page = self.reader.read_logs(cursor=cursor)
            results.append(page)
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertNotIn("A" * 64, json.dumps(results))
        self.assertIn("ordinary", json.dumps(results))

    def test_private_key_header_inside_oversized_record_does_not_expose_body(self):
        self.config.max_bytes = 2048
        self.reader = Diagnostics(self.config)
        Path(self.config.log_path).write_text("x" * 505 + "-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n-----END PRIVATE KEY-----\nordinary\n")
        results, cursor = [], None
        for _ in range(10):
            page = self.reader.read_logs(cursor=cursor)
            results.append(page)
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertNotIn("A" * 64, json.dumps(results))

    def test_risk_output_can_use_a_different_provider_from_the_review(self):
        job = self.job(provider="claude")
        path = self.root / "runs" / str(job) / "risk"
        path.mkdir(parents=True)
        (path / "codex-risk-stdout.log").write_text("risk outcome\n")
        self.assertEqual(self.reader.read_run_output(job, stage="risk", provider="codex")["content"], "risk outcome")
        self.assertEqual(self.reader.read_run_output(job, stage="risk")["content"], "risk outcome")

    def test_selection_output_requires_associated_round_and_uses_selected_provider(self):
        pr = PullRequest("ws", "repo", 1, "Review", "", "feature", "a" * 40, "main")
        round_ = self.store.inline.create_round(pr, ["claude"], "v1", "v1")
        job = self.reader.list_jobs()["jobs"][0]["id"]
        path = self.root / "runs" / "selection" / round_["id"]
        path.mkdir(parents=True)
        (path / "codex-selection-final-message.json").write_text("{\"decisions\": []}")
        result = self.reader.read_run_output(job, round_id=round_["id"], stage="selection", artifact="final_message", provider="codex")
        self.assertEqual(result["content"], "{\"decisions\": []}")
        self.assertEqual(self.reader.read_run_output(job, round_id="0" * 32, stage="selection")["status"], "not_found")
        self.assertEqual(self.reader.read_run_output(job, round_id="../../secret", stage="selection")["status"], "invalid_request")

    @unittest.skipIf(os.geteuid() == 0, "Requires an unprivileged reader to enforce directory modes")
    def test_log_reads_need_only_traversal_permission_on_parent_directories(self):
        directory = self.root / "traverse-only"
        directory.mkdir()
        path = directory / "daemon.log"
        path.write_text("readable through execute-only parent\n")
        path.chmod(0o400)
        self.config.log_path = str(path)
        directory.chmod(0o100)
        try:
            with self.assertRaises(PermissionError):
                os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
            result = Diagnostics(self.config).read_logs()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["records"], ["readable through execute-only parent"])
        finally:
            directory.chmod(0o700)

    def test_safe_open_holds_parent_descriptor_across_symlink_swap(self):
        job = self.job()
        run = self.root / "runs" / str(job)
        run.mkdir(parents=True)
        (run / "codex-stdout.log").write_text("safe output\n")
        escape = self.root / "private"
        escape.mkdir()
        (escape / "codex-stdout.log").write_text("secret outside allowed tree\n")
        real_open = os.open
        def racing_open(path, flags, *args, **kwargs):
            if path == "codex-stdout.log":
                run.rename(run.with_name("original"))
                run.symlink_to(escape)
            return real_open(path, flags, *args, **kwargs)
        with patch("scout.diagnostic_files.os.open", side_effect=racing_open):
            result = self.reader.read_run_output(job)
        self.assertEqual(result["content"], "safe output")

    def test_retry_overwrite_invalidates_output_cursor(self):
        job = self.job()
        path = self.root / "runs" / str(job)
        path.mkdir(parents=True)
        output = path / "codex-stdout.log"
        output.write_text("first\n")
        first = self.reader.read_run_output(job)
        output.write_text("replacement\n")
        self.assertEqual(self.reader.read_run_output(job, cursor=first["next_cursor"])["status"], "stale_cursor")

    def test_large_job_errors_remain_addressable_under_small_byte_budget(self):
        self.config.max_bytes = 2048
        self.reader = Diagnostics(self.config)
        job = self.job()
        with self.store.connect() as conn:
            conn.execute("update review_jobs set title=?,error_message=?", ("😃" * 10000, "x" * 10000))
        result = self.reader.list_jobs()
        self.assertEqual(result["jobs"][0]["id"], job)
        self.assertTrue(result["jobs"][0]["fields_truncated"])
        self.assertLessEqual(len(json.dumps(result).encode()), 2048)

    def test_cursor_cannot_be_reused_with_other_source_or_after_restart(self):
        Path(self.config.log_path).write_text("ordinary\n")
        cursor = self.reader.read_logs()["next_cursor"]
        self.assertEqual(self.reader.read_logs("review", cursor=cursor)["status"], "invalid_request")
        restarted = Diagnostics(self.config)
        self.assertEqual(restarted.read_logs(cursor=cursor)["status"], "invalid_request")

    def test_usage_pagination_retains_every_group_under_transport_budget(self):
        self.config.max_bytes = 24576
        self.reader = Diagnostics(self.config)
        rows = [{"workspace": "w" * 100, "repo": "r" * 100, "pr": number,
                 "provider": "codex", "timestamp": "2026-10-02T00:00:00Z",
                 "usage": {"total_tokens": 1}} for number in range(200)]
        self.write_log("usage", rows)
        cursor, tokens, groups = None, 0, 0
        for _ in range(201):
            result = self.reader.get_usage(cursor=cursor)
            self.assertLessEqual(len(json.dumps(result).encode()), 24576)
            self.assertNotIn("truncation_reason", result)
            tokens += sum(row["total_tokens"] for row in result["usage"])
            groups += len(result["usage"])
            if not result["has_more"]:
                break
            cursor = result["next_cursor"]
        self.assertEqual(tokens, 200)
        self.assertEqual(groups, 200)

    def test_query_deadline_interrupts_long_sql_without_changing_state(self):
        self.config.query_timeout_seconds = 0.001
        reader = Diagnostics(self.config)
        with reader._connect() as conn:
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                conn.execute("with recursive n(x) as (values(1) union all select x+1 from n where x<10000000) select sum(x) from n").fetchone()

    def test_scan_budget_and_record_budget_are_hard_limits(self):
        self.config.max_scan_bytes = 512
        self.reader = Diagnostics(self.config)
        Path(self.config.log_path).write_text("x" * 10000 + "\nordinary\n")
        result = self.reader.read_logs()
        self.assertLessEqual(result["scanned_bytes"], 512)
        self.assertTrue(result["truncated"])
        self.assertIsNotNone(result["next_cursor"])
        Path(self.config.log_path).write_text("ordinary\n" * 300)
        result = self.reader.read_logs(limit=999999)
        self.assertLessEqual(len(result["records"]), 200)


if __name__ == "__main__":
    unittest.main()
