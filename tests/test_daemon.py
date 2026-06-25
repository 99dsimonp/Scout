import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from scout.bitbucket import BitbucketError
from scout.daemon import (
    ScoutDaemon,
    _append_provider_usage_log_entry,
    _append_review_log_entry,
    _format_provider_model_metadata,
    _lease_seconds,
    _provider_usage_log_entry,
    _review_log_entry,
    _reap_worker_futures,
    _seconds_until_next_poll,
)
from scout.config import CredentialStore, parse_config
from scout.gitops import GitError
from scout.models import PullRequest
from scout.provider import ProviderResult
from scout.schema import validate_review_output
from scout.state import ReviewJob, StateStore


def review_job(provider="claude", job_id=7, pr_id=13, attempts=1, description="", output_mode="reports"):
    return ReviewJob(
        id=job_id,
        workspace="ws",
        repo_slug="repo",
        pr_id=pr_id,
        title="PR",
        description=description,
        source_branch="feature",
        target_source_commit_hash="a" * 40,
        running_source_commit_hash="b" * 40,
        destination_branch="main",
        destination_commit_hash=None,
        merge_base_hash=None,
        reviewer_policy_version="v1",
        schema_version="v1",
        provider=provider,
        status="running",
        superseded=False,
        attempts=attempts,
        leased_until=None,
        lease_token="lease",
        target_review_key="key",
        running_review_key="key",
        target_review_run_id="run",
        running_review_run_id="run",
        error_message=None,
        output_mode=output_mode,
    )


def valid_review():
    return validate_review_output(
        {
            "recommendation": "request_changes",
            "report": {
                "title": "Claude PR Review",
                "details": "Found one issue.",
                "report_type": "BUG",
                "reporter": "scout",
                "data": [{"title": "Findings", "type": "NUMBER", "value": 1}],
            },
            "annotations": [
                {
                    "external_id": "finding-001",
                    "annotation_type": "BUG",
                    "path": "src/app.py",
                    "line": 12,
                    "summary": "Missing error handling",
                    "details": "The changed call can fail.",
                    "severity": "HIGH",
                    "result": "FAILED",
                    "reviewer": "correctness",
                    "confidence": "HIGH",
                    "smallest_fix": "Handle the failure before updating state.",
                }
            ],
        }
    )


class DaemonReviewLogTests(unittest.TestCase):
    def test_model_metadata_uses_cli_default_when_model_is_empty(self):
        self.assertEqual(_format_provider_model_metadata("", "max"), "CLI default / max")
        self.assertEqual(_format_provider_model_metadata("claude-opus-4-8", ""), "claude-opus-4-8 / CLI default")

    def test_lease_seconds_covers_provider_timeout_with_grace(self):
        self.assertEqual(_lease_seconds(1200, 1800), 1860)
        self.assertEqual(_lease_seconds(2400, 1800), 2400)
        self.assertEqual(_lease_seconds(1200, 1800, 120), 1980)

    def test_review_log_entry_records_metadata_counts_and_log_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = str(Path(tmp) / "runs" / "7")
            entry = _review_log_entry(
                review_job(),
                "b" * 40,
                valid_review(),
                run_dir,
                {"total_tokens": 123},
            )

            self.assertIn("timestamp", entry)
            self.assertEqual(entry["provider"], "claude")
            self.assertEqual(entry["workspace"], "ws")
            self.assertEqual(entry["repo"], "repo")
            self.assertEqual(entry["pr"], 13)
            self.assertEqual(entry["commit"], "b" * 40)
            self.assertEqual(entry["recommendation"], "request_changes")
            self.assertEqual(entry["usage"], {"total_tokens": 123})
            self.assertEqual(entry["findings_count"], 1)
            self.assertEqual(entry["findings_summary"]["by_reviewer"], {"correctness": 1})
            self.assertEqual(
                entry["raw_provider_logs"],
                {
                    "stdout": str(Path(run_dir) / "claude-stdout.log"),
                    "stderr": str(Path(run_dir) / "claude-stderr.log"),
                },
            )

    def test_codex_review_log_entry_includes_final_message_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = str(Path(tmp) / "runs" / "7")
            entry = _review_log_entry(review_job(provider="codex"), "b" * 40, valid_review(), run_dir)

            self.assertEqual(
                entry["raw_provider_logs"]["final_message"],
                str(Path(run_dir) / "codex-final-message.json"),
            )

    def test_append_review_log_entry_writes_jsonl_under_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = _review_log_entry(review_job(), "b" * 40, valid_review(), str(Path(tmp) / "runs" / "7"))

            path = _append_review_log_entry(tmp, entry)

            self.assertEqual(path, Path(tmp) / "review-log.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), entry)

    def test_append_provider_usage_log_entry_writes_jsonl_under_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = _provider_usage_log_entry(
                review_job(provider="codex"),
                "b" * 40,
                str(Path(tmp) / "runs" / "7"),
                "provider_completed",
                {"total_tokens": 12345},
            )

            path = _append_provider_usage_log_entry(tmp, entry)

            self.assertEqual(path, Path(tmp) / "provider-usage.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            written = json.loads(lines[0])
            self.assertEqual(written["provider"], "codex")
            self.assertEqual(written["pr"], 13)
            self.assertEqual(written["status"], "provider_completed")
            self.assertEqual(written["usage"]["total_tokens"], 12345)

    def test_reap_worker_futures_logs_and_suppresses_worker_exception(self):
        future = Future()
        future.set_exception(RuntimeError("boom"))
        futures = {future: 7}

        with self.assertLogs("scout.daemon", level="ERROR") as logs:
            _reap_worker_futures([future], futures)

        self.assertEqual(futures, {})
        self.assertTrue(any("review worker failed unexpectedly job=7" in message for message in logs.output))

    def test_seconds_until_next_poll_uses_remaining_interval_after_worker_completion(self):
        self.assertEqual(_seconds_until_next_poll(1600.0, 1000.0), 600.0)
        self.assertEqual(_seconds_until_next_poll(1600.0, 1200.0), 400.0)
        self.assertEqual(_seconds_until_next_poll(1600.0, 1600.0), 0.0)
        self.assertEqual(_seconds_until_next_poll(1600.0, 1700.0), 0.0)

    def test_initialize_recovers_abandoned_jobs_after_state_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                service=SimpleNamespace(state_dir=tmp, retention_days=7),
                bitbucket=SimpleNamespace(
                    workspace="ws",
                    repositories=[SimpleNamespace(slug="repo", clone_url="ssh://repo")],
                ),
            )
            daemon.state = _FakeStartupState(recovered=2)
            daemon.providers = {"codex": _FakeStartupProvider()}
            daemon.bitbucket = _FakeStartupBitbucket()
            daemon.git = _FakeStartupGit()
            daemon.cleanup_old_artifacts = lambda: daemon.state.calls.append("cleanup")

            with self.assertLogs("scout.daemon", level="INFO") as logs:
                daemon.initialize()

            self.assertEqual(
                daemon.state.calls,
                ["initialize", ("upsert", "ws", "repo", "ssh://repo"), "recover", "cleanup"],
            )
            self.assertEqual(daemon.providers["codex"].calls, ["validate_startup"])
            self.assertEqual(daemon.bitbucket.validated, ["repo"])
            self.assertEqual(daemon.git.validated, ["ssh://repo"])
            self.assertTrue(
                any("recovered abandoned active review jobs count=2" in message for message in logs.output)
            )

    def test_init_loads_risk_provider_without_adding_review_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bitbucket_username").write_text("user\n", encoding="utf-8")
            Path(tmp, "bitbucket_api_key").write_text("key\n", encoding="utf-8")
            config = parse_config(
                {
                    "service": {
                        "state_db": str(Path(tmp) / "state.db"),
                        "state_dir": tmp,
                    },
                    "bitbucket": {
                        "workspace": "ws",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                    "agents": {"strategy": "claude", "claude": {"enabled": True}},
                    "review": {"risk": {"provider": "codex"}},
                }
            )

            daemon = ScoutDaemon(config, CredentialStore(tmp))

            self.assertEqual(daemon.provider_names, ["claude"])
            self.assertEqual(sorted(daemon.providers), ["claude", "codex"])
            self.assertEqual(sorted(daemon.provider_configs), ["claude", "codex"])
            self.assertEqual(daemon.max_parallel_reviews, 2)

    def test_init_uses_oauth_bitbucket_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bb_client_id").write_text("client-id\n", encoding="utf-8")
            Path(tmp, "bb_client_secret").write_text("client-secret\n", encoding="utf-8")
            config = parse_config(
                {
                    "service": {
                        "state_db": str(Path(tmp) / "state.db"),
                        "state_dir": tmp,
                    },
                    "bitbucket": {
                        "workspace": "ws",
                        "api_auth": "oauth_client_credentials",
                        "oauth_client_id_credential": "bb_client_id",
                        "oauth_client_secret_credential": "bb_client_secret",
                        "repositories": [{"slug": "repo", "clone_url": "git@bitbucket.org:ws/repo.git"}],
                    },
                }
            )

            daemon = ScoutDaemon(config, CredentialStore(tmp))

            self.assertEqual(daemon.bitbucket.credentials.auth_type, "oauth_client_credentials")
            self.assertEqual(daemon.bitbucket.credentials.oauth_client_id, "client-id")
            self.assertEqual(daemon.bitbucket.credentials.oauth_client_secret, "client-secret")

    def test_schedule_claims_from_global_queue_with_provider_specific_leases(self):
        daemon = ScoutDaemon.__new__(ScoutDaemon)
        daemon.config = SimpleNamespace(queue=SimpleNamespace(job_timeout_seconds=1200))
        daemon.provider_names = ["codex", "claude"]
        daemon.provider_configs = {
            "codex": SimpleNamespace(max_parallel=2, timeout_seconds=1200),
            "claude": SimpleNamespace(max_parallel=1, timeout_seconds=1800),
        }
        daemon.run_job = lambda job: None
        state = _FakeClaimingState(
            [
                review_job(provider="codex", job_id=1, pr_id=1),
                review_job(provider="claude", job_id=2, pr_id=1),
                review_job(provider="codex", job_id=3, pr_id=2),
            ]
        )
        daemon.state = state
        daemon.max_parallel_reviews = 2
        futures = {}
        pool = _RecordingPool()

        daemon._schedule(pool, futures)

        self.assertEqual([(job.provider, job.pr_id) for job in pool.submitted], [("codex", 1), ("claude", 1)])
        self.assertEqual(
            state.claims,
            [
                {"codex": 1260, "claude": 1860},
                {"codex": 1260, "claude": 1860},
            ],
        )
        self.assertEqual(
            sorted(metadata["provider"] for metadata in futures.values()),
            ["claude", "codex"],
        )

    def test_schedule_honors_running_provider_parallel_limit(self):
        daemon = ScoutDaemon.__new__(ScoutDaemon)
        daemon.config = SimpleNamespace(queue=SimpleNamespace(job_timeout_seconds=1200))
        daemon.provider_names = ["codex", "claude"]
        daemon.provider_configs = {
            "codex": SimpleNamespace(max_parallel=1, timeout_seconds=1200),
            "claude": SimpleNamespace(max_parallel=2, timeout_seconds=1800),
        }
        daemon.run_job = lambda job: None
        state = _FakeClaimingState(
            [
                review_job(provider="codex", job_id=1),
                review_job(provider="claude", job_id=2),
            ]
        )
        daemon.state = state
        daemon.max_parallel_reviews = 2
        running = Future()
        futures = {running: {"id": 99, "provider": "codex"}}
        pool = _RecordingPool()

        daemon._schedule(pool, futures)

        self.assertEqual([job.provider for job in pool.submitted], ["claude"])
        self.assertEqual(state.claims, [{"claude": 1860}])

    def test_poll_once_seeds_existing_provider_report_without_queueing(self):
        daemon = _polling_daemon(report_exists=True)

        daemon.poll_once()
        daemon.poll_once()

        self.assertEqual(
            daemon.bitbucket.report_checks,
            [("repo", "a" * 40, "scout-codex-v1")],
        )
        self.assertEqual(len(daemon.state.seeded), 1)
        self.assertEqual(daemon.state.enqueued, [])

    def test_empty_db_bootstrap_seeds_existing_reports_for_all_providers(self):
        daemon = _polling_daemon(report_exists=True, providers=["codex", "claude"])

        daemon.poll_once()

        self.assertEqual(
            daemon.bitbucket.report_checks,
            [
                ("repo", "a" * 40, "scout-codex-v1"),
                ("repo", "a" * 40, "scout-claude-v1"),
            ],
        )
        self.assertEqual(
            daemon.state.seeded,
            [
                ("repo", 13, "codex", "scout-codex-v1"),
                ("repo", 13, "claude", "scout-claude-v1"),
            ],
        )
        self.assertEqual(daemon.state.enqueued, [])

    def test_empty_sqlite_db_bootstrap_seeds_existing_reports_without_pending_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(str(Path(tmp) / "state.db"))
            store.initialize()
            daemon = _polling_daemon(report_exists=True, providers=["codex", "claude"])
            daemon.state = store

            daemon.poll_once()
            daemon.poll_once()

            self.assertEqual(
                daemon.bitbucket.report_checks,
                [
                    ("repo", "a" * 40, "scout-codex-v1"),
                    ("repo", "a" * 40, "scout-claude-v1"),
                ],
            )
            self.assertEqual(store.claim_pending_jobs(10, 1200), [])
            with store.connect() as conn:
                rows = conn.execute(
                    """
                    select provider, status
                    from review_jobs
                    order by provider
                    """
                ).fetchall()
            self.assertEqual(
                [(row["provider"], row["status"]) for row in rows],
                [
                    ("claude", "succeeded"),
                    ("codex", "succeeded"),
                ],
            )

    def test_poll_once_queues_missing_provider_report_without_rechecking(self):
        daemon = _polling_daemon(report_exists=False)

        daemon.poll_once()
        daemon.poll_once()

        self.assertEqual(
            daemon.bitbucket.report_checks,
            [("repo", "a" * 40, "scout-codex-v1")],
        )
        self.assertEqual(daemon.state.seeded, [])
        self.assertEqual(len(daemon.state.enqueued), 1)

    def test_poll_once_queues_after_one_failed_report_bootstrap(self):
        daemon = _polling_daemon(report_exists=BitbucketError("rate limited", retryable=True))

        daemon.poll_once()
        daemon.poll_once()

        self.assertEqual(
            daemon.bitbucket.report_checks,
            [("repo", "a" * 40, "scout-codex-v1")],
        )
        self.assertEqual(daemon.state.seeded, [])
        self.assertEqual(len(daemon.state.enqueued), 1)

    def test_poll_once_skips_and_clears_state_for_ignored_source_branches(self):
        ignored_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=14,
            title="Ignored PR",
            description="",
            source_branch="release/1.0",
            source_commit_hash="b" * 40,
            destination_branch="main",
        )
        regular_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=13,
            title="Regular PR",
            description="",
            source_branch="feature",
            source_commit_hash="a" * 40,
            destination_branch="main",
        )

        daemon = _polling_daemon(report_exists=False)
        daemon.config.bitbucket.repositories[0].ignored_source_branches = ["^release/"]
        daemon.bitbucket.prs = [ignored_pr, regular_pr]

        daemon.poll_once()

        self.assertEqual(
            daemon.state.pruned_ignored,
            [("ws", "repo", [14])],
        )
        self.assertEqual(len(daemon.state.enqueued), 1)
        self.assertEqual(daemon.state.enqueued, [("repo", 13, "codex")])

    def test_poll_once_clears_ignored_source_branches_before_pr_id_filter(self):
        ignored_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=14,
            title="Ignored PR",
            description="",
            source_branch="release/1.0",
            source_commit_hash="b" * 40,
            destination_branch="main",
        )
        regular_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=13,
            title="Regular PR",
            description="",
            source_branch="feature",
            source_commit_hash="a" * 40,
            destination_branch="main",
        )

        daemon = _polling_daemon(report_exists=False)
        daemon.config.bitbucket.repositories[0].ignored_source_branches = ["^release/"]
        daemon.config.bitbucket.repositories[0].pr_ids = [13]
        daemon.bitbucket.prs = [ignored_pr, regular_pr]

        daemon.poll_once()

        self.assertEqual(
            daemon.state.pruned_ignored,
            [("ws", "repo", [14])],
        )
        self.assertEqual(daemon.state.enqueued, [("repo", 13, "codex")])

    def test_poll_once_skips_and_clears_state_for_ignored_target_branches(self):
        ignored_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=14,
            title="Ignored PR",
            description="",
            source_branch="feature",
            source_commit_hash="b" * 40,
            destination_branch="release/1.0",
        )
        regular_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=13,
            title="Regular PR",
            description="",
            source_branch="feature",
            source_commit_hash="a" * 40,
            destination_branch="main",
        )

        daemon = _polling_daemon(report_exists=False)
        daemon.config.bitbucket.repositories[0].ignored_target_branches = ["^release/"]
        daemon.bitbucket.prs = [ignored_pr, regular_pr]

        daemon.poll_once()

        self.assertEqual(
            daemon.state.pruned_ignored,
            [("ws", "repo", [14])],
        )
        self.assertEqual(daemon.state.enqueued, [("repo", 13, "codex")])

    def test_poll_once_clears_ignored_target_branches_before_pr_id_filter(self):
        ignored_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=14,
            title="Ignored PR",
            description="",
            source_branch="feature",
            source_commit_hash="b" * 40,
            destination_branch="release/1.0",
        )
        regular_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=13,
            title="Regular PR",
            description="",
            source_branch="feature",
            source_commit_hash="a" * 40,
            destination_branch="main",
        )

        daemon = _polling_daemon(report_exists=False)
        daemon.config.bitbucket.repositories[0].ignored_target_branches = ["^release/"]
        daemon.config.bitbucket.repositories[0].pr_ids = [13]
        daemon.bitbucket.prs = [ignored_pr, regular_pr]

        daemon.poll_once()

        self.assertEqual(
            daemon.state.pruned_ignored,
            [("ws", "repo", [14])],
        )
        self.assertEqual(daemon.state.enqueued, [("repo", 13, "codex")])

    def test_poll_once_skips_and_clears_state_for_draft_prs(self):
        ignored_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=14,
            title="Draft PR",
            description="",
            source_branch="feature",
            source_commit_hash="b" * 40,
            destination_branch="main",
            is_draft=True,
        )
        regular_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=13,
            title="Regular PR",
            description="",
            source_branch="feature",
            source_commit_hash="a" * 40,
            destination_branch="main",
        )

        daemon = _polling_daemon(report_exists=False)
        daemon.config.bitbucket.repositories[0].ignore_draft_pull_requests = True
        daemon.bitbucket.prs = [ignored_pr, regular_pr]

        daemon.poll_once()

        self.assertEqual(
            daemon.state.pruned_ignored,
            [("ws", "repo", [14])],
        )
        self.assertEqual(len(daemon.state.enqueued), 1)
        self.assertEqual(daemon.state.enqueued, [("repo", 13, "codex")])

    def test_poll_once_prunes_closed_prs_after_successful_unfiltered_poll(self):
        daemon = _polling_daemon(report_exists=True)

        daemon.poll_once()

        self.assertEqual(daemon.state.pruned, [("ws", "repo", [13])])

    def test_poll_once_does_not_prune_when_repo_is_pr_id_filtered(self):
        daemon = _polling_daemon(report_exists=True)
        daemon.config.bitbucket.repositories[0].pr_ids = [13]

        daemon.poll_once()

        self.assertEqual(daemon.state.pruned, [])

    def test_inline_poll_once_skips_drafts_even_without_repo_draft_filter(self):
        draft_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=14,
            title="Draft PR",
            description="",
            source_branch="feature",
            source_commit_hash="b" * 40,
            destination_branch="main",
            is_draft=True,
        )
        regular_pr = PullRequest(
            workspace="ws",
            repo_slug="repo",
            pr_id=13,
            title="Regular PR",
            description="",
            source_branch="feature",
            source_commit_hash="a" * 40,
            destination_branch="main",
        )
        daemon = _polling_daemon(report_exists=False, output_mode="inline_comments")
        daemon.bitbucket.prs = [draft_pr, regular_pr]

        daemon.poll_once()

        self.assertEqual(daemon.state.pruned_ignored, [("ws", "repo", [14])])
        self.assertEqual(daemon.bitbucket.report_checks, [])
        self.assertEqual(daemon.state.enqueued, [("repo", 13, "codex", "inline_comments")])

    def test_inline_poll_once_force_enqueues_when_scout_comment_requests_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _polling_daemon(report_exists=False, providers=["codex", "claude"], output_mode="inline_comments")
            daemon.config.service = SimpleNamespace(state_dir=tmp)
            daemon.config.review.request_comments = SimpleNamespace(
                provider="codex",
                model="gpt-5.4",
                effort="low",
                timeout_seconds=30,
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(max_parallel=1),
                "claude": SimpleNamespace(max_parallel=1),
            }
            classifier = _FakeProvider(review_requested=True)
            daemon.providers = {"codex": classifier, "claude": _FakeProvider()}
            daemon.bitbucket.comments_by_pr = {
                ("repo", 13): [
                    {
                        "id": 1001,
                        "updated_on": "2026-01-01T00:00:00+00:00",
                        "content": {"raw": "@Scout please review this again"},
                    }
                ]
            }
            for provider in ("codex", "claude"):
                daemon.state.known.add(("repo", 13, "a" * 40, provider, "inline_comments"))

            daemon.poll_once()
            daemon.poll_once()

            self.assertEqual(classifier.review_request_calls, ["@Scout please review this again"])
            self.assertEqual(
                daemon.state.force_enqueued,
                [
                    ("repo", 13, "codex", "inline_comments"),
                    ("repo", 13, "claude", "inline_comments"),
                ],
            )
            self.assertEqual(
                daemon.state.processed_comments,
                {("ws", "repo", 13, "1001", "2026-01-01T00:00:00+00:00"): True},
            )

    def test_inline_poll_once_marks_non_request_scout_mention_without_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _polling_daemon(report_exists=False, output_mode="inline_comments")
            daemon.config.service = SimpleNamespace(state_dir=tmp)
            daemon.config.review.request_comments = SimpleNamespace(
                provider="codex",
                model="gpt-5.4",
                effort="low",
                timeout_seconds=30,
            )
            daemon.provider_configs = {"codex": SimpleNamespace(max_parallel=1)}
            classifier = _FakeProvider(review_requested=False)
            daemon.providers = {"codex": classifier}
            daemon.bitbucket.comments_by_pr = {
                ("repo", 13): [
                    {
                        "id": 1002,
                        "updated_on": "2026-01-02T00:00:00+00:00",
                        "content": {"raw": "Thanks @scout, that explains it."},
                    }
                ]
            }
            daemon.state.known.add(("repo", 13, "a" * 40, "codex", "inline_comments"))

            daemon.poll_once()

            self.assertEqual(classifier.review_request_calls, ["Thanks @scout, that explains it."])
            self.assertEqual(daemon.state.force_enqueued, [])
            self.assertEqual(
                daemon.state.processed_comments,
                {("ws", "repo", 13, "1002", "2026-01-02T00:00:00+00:00"): False},
            )

    def test_run_job_uses_job_provider_runner_config_and_report_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=2,
                    timeout_seconds=1200,
                    model="gpt-5.5",
                    reasoning_effort="high",
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
                "claude": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1800,
                    model="claude-opus-4-8",
                    effort="max",
                    max_subagents=5,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=1,
                ),
            }
            codex = _FakeProvider()
            claude = _FakeProvider()
            daemon.providers = {"codex": codex, "claude": claude}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="claude", job_id=9))

            self.assertEqual(codex.runs, [])
            self.assertEqual(len(claude.runs), 1)
            self.assertEqual(daemon.git.suffix, "job-9")
            self.assertIn("- Subagents per review category: 1", claude.runs[0]["prompt"])
            self.assertEqual(
                daemon.state.publishing_leases,
                [("claude", 1860)],
            )
            self.assertEqual(
                daemon.state.successes,
                [("claude", "scout-claude-v1")],
            )
            self.assertEqual(daemon.bitbucket.reports[0][2], "scout-claude-v1")
            self.assertEqual(daemon.bitbucket.reports[0][3]["title"], "Claude PR Review")
            self.assertIn(
                {"title": "Model", "type": "TEXT", "value": "claude-opus-4-8 / max"},
                daemon.bitbucket.reports[0][3]["data"],
            )
            self.assertEqual(daemon.bitbucket.annotations[0][2], "scout-claude-v1")

    def test_run_job_reuses_risk_classification_across_provider_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    risk=SimpleNamespace(
                        enabled=True,
                        provider="codex",
                        model="gpt-5.4",
                        effort="low",
                        timeout_seconds=12,
                    ),
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=2,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
                "claude": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1800,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
            }
            codex = _FakeProvider(risk="high")
            claude = _FakeProvider()
            daemon.providers = {"codex": codex, "claude": claude}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=21, description="Touches auth."))
            daemon.run_job(review_job(provider="claude", job_id=22, description="Touches auth."))

            self.assertEqual(
                codex.risk_calls,
                [
                    {
                        "description": "Touches auth.",
                        "model": "gpt-5.4",
                        "reasoning_effort": "low",
                        "timeout_seconds": 12,
                    }
                ],
            )
            self.assertIn("- PR description risk: high", codex.runs[0]["prompt"])
            self.assertIn("- Subagents per review category: 3", codex.runs[0]["prompt"])
            self.assertIn("- PR description risk: high", claude.runs[0]["prompt"])
            self.assertIn("- Subagents per review category: 3", claude.runs[0]["prompt"])

    def test_run_job_defaults_to_medium_risk_when_classifier_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    risk=SimpleNamespace(
                        enabled=True,
                        provider="codex",
                        model="gpt-5.4",
                        effort="low",
                        timeout_seconds=12,
                    ),
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
            }
            provider = _FakeProvider(risk_error=RuntimeError("classification failed"))
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=23, description="Touches auth."))

            self.assertEqual(len(provider.risk_calls), 1)
            self.assertIn("- PR description risk: medium", provider.runs[0]["prompt"])
            self.assertIn("- Subagents per review category: 2", provider.runs[0]["prompt"])

    def test_run_job_skips_risk_classification_when_risk_provider_is_cooling_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    risk=SimpleNamespace(
                        enabled=True,
                        provider="codex",
                        model="gpt-5.4",
                        effort="low",
                        timeout_seconds=12,
                    ),
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
                "claude": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1800,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
            }
            codex = _FakeProvider(risk="high")
            claude = _FakeProvider()
            daemon.providers = {"codex": codex, "claude": claude}
            daemon.state = _FakeRunJobState(cooldowns={"codex": "2099-01-01T00:00:00+00:00"})
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="claude", job_id=24, description="Touches auth."))

            self.assertEqual(codex.risk_calls, [])
            self.assertIn("- PR description risk: medium", claude.runs[0]["prompt"])
            self.assertIn("- Subagents per review category: 2", claude.runs[0]["prompt"])

    def test_run_job_skips_risk_classification_when_risk_provider_capacity_is_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    risk=SimpleNamespace(
                        enabled=True,
                        provider="codex",
                        model="gpt-5.4",
                        effort="low",
                        timeout_seconds=12,
                    ),
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
                "claude": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1800,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=1,
                    subagent_max_per_lens=4,
                ),
            }
            codex = _FakeProvider(risk="high")
            claude = _FakeProvider()
            daemon.providers = {"codex": codex, "claude": claude}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            self.assertTrue(daemon._acquire_provider_slot("codex", blocking=False))
            try:
                daemon.run_job(review_job(provider="claude", job_id=25, description="Touches auth."))
            finally:
                daemon._release_provider_slot("codex")

            self.assertEqual(codex.risk_calls, [])
            self.assertIn("- PR description risk: medium", claude.runs[0]["prompt"])
            self.assertIn("- Subagents per review category: 2", claude.runs[0]["prompt"])

    def test_run_job_comments_on_critical_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    model="gpt-5.5",
                    reasoning_effort="high",
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found one issue.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "Critical data loss",
                            "details": "The changed call can lose committed data.",
                            "severity": "CRITICAL",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Persist the update before acknowledging.",
                        }
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=11))

            self.assertEqual(len(daemon.bitbucket.comments), 1)
            self.assertIn(
                {"title": "Model", "type": "TEXT", "value": "gpt-5.5 / high"},
                daemon.bitbucket.reports[0][3]["data"],
            )
            repo_slug, pr_id, content = daemon.bitbucket.comments[0]
            self.assertEqual((repo_slug, pr_id), ("repo", 13))
            self.assertEqual(daemon.bitbucket.operations, ["report", "annotations", "comment"])
            self.assertIn("Scout: Critical issue found by Codex:", content)
            self.assertIn("Critical data loss", content)
            self.assertEqual(daemon.state.renewals[-2:], [("codex", 1260), ("codex", 1260)])

    def test_run_job_does_not_comment_on_non_critical_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found one issue.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "Important but not critical",
                            "details": "The changed call can fail.",
                            "severity": "HIGH",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Handle the failure.",
                        }
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=12))

            self.assertEqual(daemon.bitbucket.comments, [])
            self.assertEqual(daemon.bitbucket.operations, ["report", "annotations"])

    def test_run_job_comments_on_configured_high_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True, severities=["HIGH"]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found one issue.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "Important but not critical",
                            "details": "The changed call can fail.",
                            "severity": "HIGH",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Handle the failure.",
                        }
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=14))

            self.assertEqual(len(daemon.bitbucket.comments), 1)
            self.assertIn("Scout: High issue found by Codex:", daemon.bitbucket.comments[0][2])
            self.assertIn("Important but not critical", daemon.bitbucket.comments[0][2])

    def test_run_job_respects_disabled_critical_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=False),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found one issue.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "Critical data loss",
                            "details": "The changed call can lose committed data.",
                            "severity": "CRITICAL",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Persist the update before acknowledging.",
                        }
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=13))

            self.assertEqual(daemon.bitbucket.comments, [])
            self.assertEqual(daemon.bitbucket.operations, ["report", "annotations"])

    def test_run_job_inline_mode_comments_on_every_annotation_without_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(critical_enabled=True, severities=["CRITICAL"]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found two issues.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "High issue",
                            "details": "The changed call can fail.",
                            "severity": "HIGH",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Handle the failure.",
                        },
                        {
                            "external_id": "finding-002",
                            "annotation_type": "CODE_SMELL",
                            "path": "src/app.py",
                            "line": 20,
                            "summary": "Low issue",
                            "details": "The changed branch is confusing.",
                            "severity": "LOW",
                            "result": "FAILED",
                            "reviewer": "best-practices",
                            "confidence": "MEDIUM",
                            "smallest_fix": "Rename the branch.",
                        },
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=15, output_mode="inline_comments"))

            self.assertEqual(daemon.bitbucket.reports, [])
            self.assertEqual(daemon.bitbucket.annotations, [])
            self.assertEqual(daemon.bitbucket.comments, [])
            self.assertEqual(
                [(item[2], item[3]) for item in daemon.bitbucket.inline_comments],
                [("src/app.py", 12), ("src/app.py", 20)],
            )
            self.assertIn("High issue", daemon.bitbucket.inline_comments[0][4])
            self.assertIn("Low issue", daemon.bitbucket.inline_comments[1][4])
            self.assertEqual(daemon.bitbucket.operations, ["inline_comment", "inline_comment"])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_run_job_inline_mode_comments_when_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(severities=[]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "approve",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "No issues.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=18, output_mode="inline_comments"))

            self.assertEqual(daemon.bitbucket.reports, [])
            self.assertEqual(daemon.bitbucket.annotations, [])
            self.assertEqual(daemon.bitbucket.inline_comments, [])
            self.assertEqual(
                daemon.bitbucket.comments,
                [("repo", 13, "Scout: Codex reviewed this pull request and found no material issues.")],
            )
            self.assertEqual(daemon.bitbucket.operations, ["comment"])
            self.assertEqual(daemon.state.marked_inline_comments, ["__scout_no_findings__"])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_run_job_inline_mode_deduplicates_no_findings_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(severities=[]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "approve",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "No issues.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState(published_inline_comments={"__scout_no_findings__"})
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=19, output_mode="inline_comments"))

            self.assertEqual(daemon.bitbucket.comments, [])
            self.assertEqual(daemon.bitbucket.inline_comments, [])
            self.assertEqual(daemon.bitbucket.operations, [])
            self.assertEqual(daemon.state.marked_inline_comments, [])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_run_job_inline_mode_deduplicates_existing_no_findings_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(severities=[]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "approve",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "No issues.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.bitbucket.existing_comments.append(
                {
                    "content": {
                        "raw": "Scout: Codex reviewed this pull request and found no material issues.",
                    },
                    "user": {"nickname": "scout-bot"},
                }
            )
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=20, output_mode="inline_comments"))

            self.assertEqual(daemon.bitbucket.comments, [])
            self.assertEqual(daemon.bitbucket.inline_comments, [])
            self.assertEqual(daemon.bitbucket.operations, [])
            self.assertEqual(daemon.state.marked_inline_comments, ["__scout_no_findings__"])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_run_job_inline_mode_ignores_user_no_findings_comment_for_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(severities=[]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "approve",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "No issues.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.bitbucket.existing_comments.append(
                {
                    "content": {
                        "raw": "Scout: Codex reviewed this pull request and found no material issues.",
                    },
                    "user": {"nickname": "alice"},
                }
            )
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=21, output_mode="inline_comments"))

            self.assertEqual(
                daemon.bitbucket.comments,
                [("repo", 13, "Scout: Codex reviewed this pull request and found no material issues.")],
            )
            self.assertEqual(daemon.bitbucket.operations, ["comment"])
            self.assertEqual(daemon.state.marked_inline_comments, ["__scout_no_findings__"])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_run_job_inline_mode_deduplicates_from_publication_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(severities=[]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found one issue.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "High issue",
                            "details": "The changed call can fail.",
                            "severity": "HIGH",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Handle the failure.",
                        }
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState(published_inline_comments={"finding-001"})
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=16, output_mode="inline_comments"))

            self.assertEqual(len(daemon.bitbucket.inline_comments), 0)
            self.assertEqual(daemon.bitbucket.operations, [])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_run_job_inline_mode_ignores_existing_comment_text_for_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(
                    policy_version="v1",
                    schema_path="/tmp/schema.json",
                    max_findings=100,
                    output_mode="inline_comments",
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                ),
                service=SimpleNamespace(state_dir=tmp),
                reports=_FakeReports(),
                comments=SimpleNamespace(severities=[]),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(
                    max_parallel=1,
                    timeout_seconds=1200,
                    max_subagents=20,
                    subagent_small_loc_limit=150,
                    subagent_medium_loc_limit=600,
                    subagent_large_loc_limit=1500,
                    subagent_high_risk_bonus=0,
                    subagent_max_per_lens=2,
                ),
            }
            provider = _FakeProvider(
                {
                    "recommendation": "request_changes",
                    "report": {
                        "title": "Codex PR Review",
                        "details": "Found one issue.",
                        "report_type": "BUG",
                        "reporter": "scout",
                        "data": [],
                    },
                    "annotations": [
                        {
                            "external_id": "finding-001",
                            "annotation_type": "BUG",
                            "path": "src/app.py",
                            "line": 12,
                            "summary": "High issue",
                            "details": "The changed call can fail.",
                            "severity": "HIGH",
                            "result": "FAILED",
                            "reviewer": "correctness",
                            "confidence": "HIGH",
                            "smallest_fix": "Handle the failure.",
                        }
                    ],
                }
            )
            daemon.providers = {"codex": provider}
            daemon.state = _FakeRunJobState()
            daemon.git = _FakeGit()
            daemon.bitbucket = _FakeBitbucket()
            daemon.bitbucket.existing_comments.append(
                {
                    "content": {
                        "raw": "Scout: High issue found by Codex. Reviewer: Correctness / HIGH confidence",
                    },
                    "user": {"nickname": "alice"},
                }
            )
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", job_id=17, output_mode="inline_comments"))

            self.assertEqual(len(daemon.bitbucket.inline_comments), 1)
            self.assertEqual(daemon.bitbucket.operations, ["inline_comment"])
            self.assertEqual(daemon.state.successes, [("codex", "inline-comments")])

    def test_git_failure_stays_retryable_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = ScoutDaemon.__new__(ScoutDaemon)
            daemon.config = SimpleNamespace(
                queue=SimpleNamespace(
                    job_timeout_seconds=1200,
                    max_attempts=3,
                    retry_backoff_seconds=300,
                ),
                review=SimpleNamespace(schema_path="/tmp/schema.json"),
                service=SimpleNamespace(state_dir=tmp),
            )
            daemon.provider_configs = {
                "codex": SimpleNamespace(timeout_seconds=1200),
            }
            daemon.providers = {"codex": _FakeProvider()}
            daemon.state = _FakeRunJobState()
            daemon.git = _FailingGit(GitError("ssh: Could not resolve hostname bitbucket.org"))
            daemon.bitbucket = _FakeBitbucket()
            daemon.clone_urls = {"repo": "git@bitbucket.org:ws/repo.git"}

            daemon.run_job(review_job(provider="codex", attempts=3))

            self.assertEqual(daemon.state.retryable_failures, [(7, 3, 300)])


def _polling_daemon(report_exists, providers=None, output_mode="reports"):
    pr = PullRequest(
        workspace="ws",
        repo_slug="repo",
        pr_id=13,
        title="PR",
        description="",
        source_branch="feature",
        source_commit_hash="a" * 40,
        destination_branch="main",
    )
    daemon = ScoutDaemon.__new__(ScoutDaemon)
    daemon.config = SimpleNamespace(
        polling=SimpleNamespace(enabled=True, pagelen=50),
        bitbucket=SimpleNamespace(
            workspace="ws",
            repositories=[SimpleNamespace(slug="repo", pr_ids=[], ignored_source_branches=[])],
        ),
        review=SimpleNamespace(policy_version="v1"),
        service=SimpleNamespace(state_dir="/tmp/scout-test"),
        reports=_FakeReports(),
    )
    daemon.config.review.output_mode = output_mode
    daemon.provider_names = list(providers or ["codex"])
    daemon.bitbucket = _FakePollingBitbucket([pr], report_exists)
    daemon.state = _FakeBootstrapState()
    return daemon


class _RecordingPool:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, job):
        self.submitted.append(job)
        return Future()


class _FakeClaimingState:
    def __init__(self, jobs):
        self.jobs = jobs
        self.claims = []

    def claim_next_pending_job(self, lease_seconds_by_provider):
        self.claims.append(dict(lease_seconds_by_provider))
        for index, job in enumerate(self.jobs):
            if job.provider in lease_seconds_by_provider:
                return self.jobs.pop(index)
        return None


class _FakeStartupState:
    def __init__(self, recovered):
        self.recovered = recovered
        self.calls = []

    def initialize(self):
        self.calls.append("initialize")

    def recover_abandoned_jobs(self):
        self.calls.append("recover")
        return self.recovered

    def upsert_repository(self, workspace, repo_slug, clone_url):
        self.calls.append(("upsert", workspace, repo_slug, clone_url))


class _FakeStartupProvider:
    def __init__(self):
        self.calls = []

    def validate_startup(self):
        self.calls.append("validate_startup")


class _FakeStartupBitbucket:
    def __init__(self):
        self.validated = []

    def validate_repository(self, repo_slug):
        self.validated.append(repo_slug)


class _FakeStartupGit:
    def __init__(self):
        self.validated = []

    def validate_clone_url(self, clone_url):
        self.validated.append(clone_url)


class _FakeBootstrapState:
    def __init__(self):
        self.known = set()
        self.bootstrap_attempts = set()
        self.seeded = []
        self.enqueued = []
        self.force_enqueued = []
        self.processed_comments = {}
        self.pruned = []
        self.pruned_ignored = []

    def has_review_for_key(self, pr, policy_version, schema_version, provider, output_mode="reports"):
        return (pr.repo_slug, pr.pr_id, pr.source_commit_hash, provider, output_mode) in self.known

    def seed_successful_review(self, pr, policy_version, schema_version, provider, report_id, output_mode="reports"):
        self.seeded.append((pr.repo_slug, pr.pr_id, provider, report_id))
        self.known.add((pr.repo_slug, pr.pr_id, pr.source_commit_hash, provider, output_mode))

    def should_bootstrap_report(self, pr, policy_version, schema_version, provider, output_mode="reports"):
        return (pr.repo_slug, pr.pr_id, pr.source_commit_hash, provider, output_mode) not in self.bootstrap_attempts

    def mark_report_bootstrap_attempted(
        self, pr, policy_version, schema_version, provider, error_message=None, output_mode="reports"
    ):
        self.bootstrap_attempts.add((pr.repo_slug, pr.pr_id, pr.source_commit_hash, provider, output_mode))

    def enqueue_or_update_pr(self, pr, policy_version, schema_version, provider, output_mode="reports"):
        if self.has_review_for_key(pr, policy_version, schema_version, provider, output_mode=output_mode):
            return False
        if output_mode == "reports":
            self.enqueued.append((pr.repo_slug, pr.pr_id, provider))
        else:
            self.enqueued.append((pr.repo_slug, pr.pr_id, provider, output_mode))
        self.known.add((pr.repo_slug, pr.pr_id, pr.source_commit_hash, provider, output_mode))
        return True

    def force_enqueue_pr_review(self, pr, policy_version, schema_version, provider, output_mode="reports"):
        self.force_enqueued.append((pr.repo_slug, pr.pr_id, provider, output_mode))
        self.known.add((pr.repo_slug, pr.pr_id, pr.source_commit_hash, provider, output_mode))
        return True

    def processed_pull_request_comment_review_requested(self, workspace, repo_slug, pr_id, comment_id, updated_on):
        return self.processed_comments.get((workspace, repo_slug, pr_id, str(comment_id), updated_on))

    def mark_pull_request_comment_processed(
        self,
        workspace,
        repo_slug,
        pr_id,
        comment_id,
        updated_on,
        review_requested,
    ):
        self.processed_comments[(workspace, repo_slug, pr_id, str(comment_id), updated_on)] = review_requested

    def get_active_provider_cooldown(self, provider):
        return None

    def mark_provider_cooldown(self, provider, error, cooldown_seconds, status="quota_exhausted"):
        return "2099-01-01T00:00:00+00:00"

    def prune_closed_pull_requests(self, workspace, repo_slug, open_pr_ids):
        self.pruned.append((workspace, repo_slug, list(open_pr_ids)))
        return 0

    def prune_ignored_pull_requests(self, workspace, repo_slug, ignored_pr_ids, ignore_reason=None):
        self.pruned_ignored.append((workspace, repo_slug, list(ignored_pr_ids)))
        return 0


class _FakePollingBitbucket:
    def __init__(self, prs, report_exists):
        self.prs = prs
        self.report_exists_result = report_exists
        self.report_checks = []
        self.comments_by_pr = {}

    def list_open_pull_requests(self, repo_slug, pagelen=50):
        return list(self.prs)

    def report_exists(self, repo_slug, commit_hash, report_id):
        self.report_checks.append((repo_slug, commit_hash, report_id))
        if isinstance(self.report_exists_result, Exception):
            raise self.report_exists_result
        return self.report_exists_result

    def list_pull_request_comments(self, repo_slug, pr_id):
        return list(self.comments_by_pr.get((repo_slug, pr_id), []))


class _FakeProvider:
    def __init__(self, final_message=None, risk="medium", risk_error=None, review_requested=True):
        self.runs = []
        self.risk_calls = []
        self.review_request_calls = []
        self.final_message = final_message
        self.risk = risk
        self.risk_error = risk_error
        self.review_requested = review_requested

    def run(self, worktree, prompt, schema_path, run_dir, is_superseded):
        self.runs.append(
            {
                "worktree": worktree,
                "prompt": prompt,
                "schema_path": schema_path,
                "run_dir": run_dir,
            }
        )
        payload = self.final_message
        if payload is None:
            payload = {
                "recommendation": "approve",
                "report": {
                    "title": "Provider PR Review",
                    "details": "No issues found.",
                    "report_type": "BUG",
                    "reporter": "scout",
                    "data": [],
                },
                "annotations": [],
            }
        return ProviderResult(stdout="{}", stderr="", final_message=json.dumps(payload))

    def assess_risk(self, description, model, timeout_seconds, run_dir, is_superseded, **kwargs):
        call = {
            "description": description,
            "model": model,
            "timeout_seconds": timeout_seconds,
        }
        call.update(kwargs)
        self.risk_calls.append(call)
        if self.risk_error is not None:
            raise self.risk_error
        return self.risk

    def classify_review_request(self, comment, model, timeout_seconds, run_dir, is_superseded, **kwargs):
        self.review_request_calls.append(comment)
        return SimpleNamespace(
            review_requested=self.review_requested,
            reason="request" if self.review_requested else "not a request",
        )


class _FakeReports:
    def report_id_for(self, provider):
        return {
            "codex": "scout-codex-v1",
            "claude": "scout-claude-v1",
        }[provider]

    def title_for(self, provider):
        return {
            "codex": "Codex PR Review",
            "claude": "Claude PR Review",
        }[provider]


class _FakeRunJobState:
    def __init__(self, cooldowns=None, published_inline_comments=None):
        self.publishing_leases = []
        self.renewals = []
        self.successes = []
        self.retryable_failures = []
        self.cooldowns = dict(cooldowns or {})
        self.marked_provider_cooldowns = []
        self.published_inline_comments = set(published_inline_comments or ())
        self.marked_inline_comments = []

    def get_active_provider_cooldown(self, provider):
        return self.cooldowns.get(provider)

    def mark_provider_cooldown(self, provider, error, cooldown_seconds, status="quota_exhausted"):
        self.marked_provider_cooldowns.append((provider, cooldown_seconds, status))
        cooldown_until = "2099-01-01T00:00:00+00:00"
        self.cooldowns[provider] = cooldown_until
        return cooldown_until

    def is_job_superseded(self, job_id, lease_token=None):
        return False

    def mark_publishing(self, job, lease_seconds):
        self.publishing_leases.append((job.provider, lease_seconds))
        return True

    def renew_publishing_lease(self, job, lease_seconds):
        self.renewals.append((job.provider, lease_seconds))
        return True

    def mark_success(self, job, report_id):
        self.successes.append((job.provider, report_id))
        return True

    def mark_retryable_failure(
        self,
        job_id,
        error,
        max_attempts,
        lease_token=None,
        running_review_key=None,
        retry_backoff_seconds=0,
    ):
        self.retryable_failures.append(
            (job_id, max_attempts, retry_backoff_seconds)
        )
        return True

    def return_superseded_to_pending(self, job_id, lease_token=None):
        pass

    def inline_comment_published(self, job, external_id):
        return external_id in self.published_inline_comments

    def mark_inline_comment_published(self, job, external_id):
        self.marked_inline_comments.append(external_id)
        self.published_inline_comments.add(external_id)


class _FakeGit:
    def ensure_mirror(self, workspace, repo_slug, clone_url):
        return "/mirror"

    def create_worktree(self, mirror, pr, suffix=None):
        self.suffix = suffix
        return "/worktree"

    def prepare_context(self, mirror, worktree, pr):
        return {
            "workspace": pr.workspace,
            "repo_slug": pr.repo_slug,
            "pr_id": str(pr.pr_id),
            "title": pr.title,
            "description": pr.description,
            "source_branch": pr.source_branch,
            "source_commit": pr.source_commit_hash,
            "target_branch": pr.destination_branch,
            "target_commit": pr.destination_commit_hash or "",
            "merge_base": pr.merge_base_hash or "",
            "changed_lines": "200",
            "context_path": "/context.json",
            "files_path": "/files.txt",
            "diff_path": "/diff.patch",
        }

    def remove_worktree(self, mirror, worktree):
        pass


class _FailingGit:
    def __init__(self, exc):
        self.exc = exc

    def ensure_mirror(self, workspace, repo_slug, clone_url):
        raise self.exc


class _FakeBitbucket:
    def __init__(self):
        self.credentials = SimpleNamespace(username="scout-bot")
        self.reports = []
        self.annotations = []
        self.comments = []
        self.inline_comments = []
        self.existing_comments = []
        self.operations = []

    def publish_report(self, repo_slug, commit_hash, report_id, report):
        self.reports.append((repo_slug, commit_hash, report_id, report))
        self.operations.append("report")

    def publish_annotations(self, repo_slug, commit_hash, report_id, annotations, before_request=None):
        self.annotations.append((repo_slug, commit_hash, report_id, annotations))
        self.operations.append("annotations")

    def publish_pull_request_comment(self, repo_slug, pr_id, content, before_request=None):
        if before_request is not None:
            before_request()
        self.comments.append((repo_slug, pr_id, content))
        self.operations.append("comment")

    def list_pull_request_comments(self, repo_slug, pr_id, before_request=None):
        if before_request is not None:
            before_request()
        return list(self.existing_comments) + [
            {"content": {"raw": content}, "user": {"nickname": "scout-bot"}}
            for _, _, _, _, content in self.inline_comments
        ]

    def publish_inline_pull_request_comment(self, repo_slug, pr_id, path, line, content, before_request=None):
        if before_request is not None:
            before_request()
        self.inline_comments.append((repo_slug, pr_id, path, line, content))
        self.operations.append("inline_comment")


if __name__ == "__main__":
    unittest.main()
