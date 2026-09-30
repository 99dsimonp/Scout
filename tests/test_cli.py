import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scout.cli import main
from scout.models import PullRequest
from scout.runtime_lock import RuntimeLock
from scout.state import StateStore


class CliTests(unittest.TestCase):
    def test_resolve_publication_requires_version_and_complete_outcome(self):
        for flags in (["--resolve-publication", "one"],
                      ["--resolve-publication", "one", "--expected-version", "1", "--outcome", "published"],
                      ["--outcome", "absent"],
                      ["--retry-publication", "one", "--once"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(flags)
                self.assertEqual(error.exception.code, 2)

    def test_reset_state_db_requires_once(self):
        stderr = io.StringIO()

        with self.assertRaises(SystemExit) as raised:
            with redirect_stderr(stderr):
                main(["--config", "/does/not/matter.toml", "--reset-state-db"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--reset-state-db requires --once", stderr.getvalue())

    def test_reset_state_db_deletes_configured_db_before_once_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state.db"
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                """
[service]
state_db = "{db_path}"
state_dir = "{state_dir}"

[bitbucket]
workspace = "ws"

[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
""".format(db_path=db_path, state_dir=tmp_path),
                encoding="utf-8",
            )
            store = StateStore(str(db_path))
            store.initialize()
            pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr, "v1", "v1", "codex")
            Path(str(db_path) + "-wal").write_text("wal", encoding="utf-8")
            Path(str(db_path) + "-shm").write_text("shm", encoding="utf-8")
            Path(str(db_path) + "-journal").write_text("journal", encoding="utf-8")

            instances = []

            class FakeDaemon:
                def __init__(self, config, credentials):
                    self.config = config
                    self.state = StateStore(config.service.state_db)
                    self.calls = []
                    instances.append(self)

                def initialize(self):
                    self.calls.append("initialize")
                    self.state.initialize()

                def poll_once(self):
                    self.calls.append("poll_once")

                def run_pending_jobs(self):
                    self.calls.append("run_pending_jobs")

                def cleanup_old_artifacts(self):
                    self.calls.append("cleanup_old_artifacts")

            with patch("scout.cli.CredentialStore"), patch("scout.cli.ScoutDaemon", FakeDaemon):
                exit_code = main(["--config", str(config_path), "--once", "--reset-state-db"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                instances[0].calls,
                ["initialize", "poll_once", "run_pending_jobs", "cleanup_old_artifacts"],
            )
            with StateStore(str(db_path)).connect() as conn:
                self.assertEqual(conn.execute("select count(*) from review_jobs").fetchone()[0], 0)
                self.assertEqual(conn.execute("select count(*) from pull_request_state").fetchone()[0], 0)

    def test_reset_state_db_refuses_when_runtime_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state.db"
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                """
[service]
state_db = "{db_path}"
state_dir = "{state_dir}"

[bitbucket]
workspace = "ws"

[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
""".format(db_path=db_path, state_dir=tmp_path),
                encoding="utf-8",
            )
            store = StateStore(str(db_path))
            store.initialize()
            pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr, "v1", "v1", "codex")

            stderr = io.StringIO()
            with RuntimeLock(str(tmp_path)):
                with redirect_stderr(stderr):
                    exit_code = main(["--config", str(config_path), "--once", "--reset-state-db"])

            self.assertEqual(exit_code, 1)
            self.assertIn("reset-state-db refused:", stderr.getvalue())
            with store.connect() as conn:
                self.assertEqual(conn.execute("select count(*) from review_jobs").fetchone()[0], 1)

    def test_recover_abandoned_jobs_command_returns_active_jobs_to_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state.db"
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                """
[service]
state_db = "{db_path}"
state_dir = "{state_dir}"

[bitbucket]
workspace = "ws"

[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
""".format(db_path=db_path, state_dir=tmp_path),
                encoding="utf-8",
            )
            store = StateStore(str(db_path))
            store.initialize()
            pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr, "v1", "v1", "codex")
            job = store.claim_pending_jobs(1, 1200)[0]

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--recover-abandoned-jobs"])

            self.assertEqual(exit_code, 0)
            self.assertIn("recovered abandoned jobs: 1", stdout.getvalue())
            recovered = store.get_job(job.id)
            self.assertEqual(recovered.status, "pending")
            self.assertIsNone(recovered.lease_token)
            self.assertEqual(recovered.error_message, "Scout service stopped while this job was active")

    def test_recover_abandoned_jobs_command_skips_when_runtime_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state.db"
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                """
[service]
state_db = "{db_path}"
state_dir = "{state_dir}"

[bitbucket]
workspace = "ws"

[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
""".format(db_path=db_path, state_dir=tmp_path),
                encoding="utf-8",
            )
            store = StateStore(str(db_path))
            store.initialize()
            pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr, "v1", "v1", "codex")
            job = store.claim_pending_jobs(1, 1200)[0]

            stdout = io.StringIO()
            with RuntimeLock(str(tmp_path)):
                with redirect_stdout(stdout):
                    exit_code = main(["--config", str(config_path), "--recover-abandoned-jobs"])

            self.assertEqual(exit_code, 0)
            self.assertIn("recovery skipped:", stdout.getvalue())
            self.assertEqual(store.get_job(job.id).status, "running")

    def test_usage_summary_prints_pr_provider_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                """
[service]
state_db = "{state_dir}/state.db"
state_dir = "{state_dir}"

[bitbucket]
workspace = "ws"

[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
""".format(state_dir=tmp_path),
                encoding="utf-8",
            )
            (tmp_path / "provider-usage.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-05-18T10:00:00+00:00",
                        "workspace": "ws",
                        "repo": "repo",
                        "pr": 3,
                        "provider": "codex",
                        "commit": "abc",
                        "usage": {"total_tokens": 1234},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--usage-summary"])

            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("repo", output)
            self.assertIn("codex", output)
            self.assertIn("1234", output)

    def test_check_startup_runs_daemon_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.toml"
            config_path.write_text(
                """
[service]
state_db = "{state_dir}/state.db"
state_dir = "{state_dir}"

[bitbucket]
workspace = "ws"

[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
""".format(state_dir=tmp_path),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with patch("scout.cli.CredentialStore"), patch("scout.cli.ScoutDaemon") as daemon_class:
                daemon = daemon_class.return_value
                with redirect_stdout(stdout):
                    exit_code = main(["--config", str(config_path), "--check-startup"])

            self.assertEqual(exit_code, 0)
            daemon.initialize.assert_called_once_with()
            self.assertIn("startup checks OK", stdout.getvalue())

if __name__ == "__main__":
    unittest.main()


class PublicationCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.config_path = self.path / "config.toml"
        self.config_path.write_text('''[service]
state_db = "{0}/state.db"
state_dir = "{0}"
[bitbucket]
workspace = "ws"
[[bitbucket.repositories]]
slug = "repo"
clone_url = "git@bitbucket.org:ws/repo.git"
'''.format(self.path))
        self.store = StateStore(str(self.path / "state.db"))
        self.store.initialize()
        pr = PullRequest("ws", "repo", 1, "PR", "", "feature", "source", "main", "dest", "base")
        self.record = self.store.inline.create_round(pr, ("codex",), "v1", "v1")
        job = self.store.claim_next_pending_job({"codex": 120})
        self.store.inline.save_result(job, {"annotations": []})
        self.record = self.store.inline.claim_round(self.record["id"], 120)
        self.store.inline.save_plan(self.record["id"], self.record["lease_token"], {"retained_ids": [], "decisions": []})
        self.intent = self.store.inline.reserve_intent(self.record["id"], "clean_review", {"content": "body"}, kind="clean_review")
        sending = self.store.inline.transition_intent(self.intent["id"], self.intent["version"], "sending", lease_token=self.record["lease_token"])
        self.intent = self.store.inline.transition_intent(sending["id"], sending["version"], "unknown")

    def run_command(self, *flags):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--config", str(self.config_path), *flags])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_list_includes_version_and_resolution_works_with_running_daemon_lock(self):
        with RuntimeLock(str(self.path)):
            status, output, _ = self.run_command("--list-unresolved-publications")
            self.assertEqual(status, 0)
            listed = json.loads(output)["intents"][0]
            self.assertEqual(listed["version"], self.intent["version"])
            status, _, _ = self.run_command("--resolve-publication", self.intent["id"], "--expected-version", str(listed["version"]), "--outcome", "published", "--comment-id", "42")
        self.assertEqual(status, 0)
        self.assertEqual(self.store.inline.get_intent(self.intent["id"])["comment_ids"], ["42"])

    def test_resolution_rejects_stale_version(self):
        status, _, error = self.run_command("--resolve-publication", self.intent["id"], "--expected-version", str(self.intent["version"] - 1), "--outcome", "absent")
        self.assertEqual(status, 1)
        self.assertIn("version changed", error)
        self.assertEqual(self.store.inline.get_intent(self.intent["id"])["status"], "unknown")

    def test_retry_failed_round_keeps_saved_plan(self):
        self.store.inline.release_round(self.record["id"], self.record["lease_token"], status="publication_failed", error="delivery failed")
        plan = self.store.inline.get_plan(self.record["id"])
        status, _, _ = self.run_command("--retry-publication", self.record["id"])
        self.assertEqual(status, 0)
        self.assertEqual(self.store.inline.get_round(self.record["id"])["status"], "publishing")
        self.assertEqual(self.store.inline.get_plan(self.record["id"]), plan)
