import json
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scout.config import parse_config
from scout.daemon import ScoutDaemon
from scout.models import PullRequest
from scout.provider import ProviderError
from scout.state import StateStore
from test_daemon import _FakeGit, _FakeProvider, valid_review
from test_inline_publisher import FakeBitbucket


class InlineGit(_FakeGit):
    def resolve_review_snapshot(self, mirror, pr):
        return replace(pr, destination_commit_hash=pr.destination_commit_hash or "d" * 40, merge_base_hash="b" * 40)

    def validate_clone_url(self, clone):
        pass


class InlineBitbucket(FakeBitbucket):
    def __init__(self):
        super().__init__()
        self.prs = [PullRequest("ws", "repo", 13, "PR", "", "feature", "a" * 40, "main", "d" * 40)]

    def list_open_pull_requests(self, repo):
        return list(self.prs)

    def get_pull_request(self, repo, pr_id):
        return next(pr for pr in self.prs if pr.pr_id == pr_id)

    def validate_repository(self, repo):
        pass


class RecordingPool:
    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args):
        self.tasks.append((fn, args))
        return Future()


class InlineDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = parse_config({
            "service": {"state_db": str(Path(self.tmp.name, "state.db")), "state_dir": self.tmp.name},
            "bitbucket": {"workspace": "ws", "bot_account_id": "bot", "repositories": [{"slug": "repo", "clone_url": "ssh://repo"}]},
            "agents": {"providers": ["codex", "claude"], "claude": {"enabled": True}},
            "review": {"output_mode": "inline_comments", "risk": {"enabled": False}, "deduplication": {"enabled": False}},
        })
        daemon = ScoutDaemon.__new__(ScoutDaemon)
        daemon.config = config
        daemon.state = StateStore(config.service.state_db)
        daemon.state.initialize()
        daemon.provider_names = list(config.agents.providers)
        daemon.provider_configs = {name: getattr(config.agents, name) for name in daemon.provider_names}
        daemon.providers = {name: _FakeProvider(asdict(valid_review())) for name in daemon.provider_names}
        daemon.clone_urls = {"repo": "ssh://repo"}
        daemon.repository_configs = {"repo": config.bitbucket.repositories[0]}
        daemon.max_parallel_reviews = 2
        daemon.git = InlineGit()
        daemon.bitbucket = InlineBitbucket()
        daemon._inline_dispatch().publisher.initialize_identity()
        self.daemon = daemon

    def round(self):
        return self.daemon.state.inline.list_rounds()[-1]

    def run_provider(self, name):
        job = self.daemon.state.claim_next_pending_job({name: 1200})
        self.assertIsNotNone(job)
        self.daemon.run_job(job)
        return job

    def enable_selection(self):
        daemon = self.daemon
        daemon.config = replace(daemon.config, review=replace(daemon.config.review, deduplication=replace(daemon.config.review.deduplication, enabled=True)))
        daemon._inline_dispatch().config = daemon.config
        daemon._inline_dispatch().publisher.config = daemon.config

    def test_review_workers_save_results_and_wait_for_every_provider(self):
        self.daemon.poll_once()
        self.run_provider("codex")
        self.assertEqual(self.round()["status"], "reviewing")
        self.assertEqual(self.round()["outcomes"][0]["status"], "succeeded")
        self.assertEqual(self.daemon.bitbucket.posts, [])
        self.run_provider("claude")
        self.assertEqual(self.round()["status"], "ready_for_selection")
        self.assertEqual(self.daemon.bitbucket.posts, [])
        self.daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        # Explicitly disabled deduplication retains exact duplicates too.
        self.assertEqual(len(self.daemon.bitbucket.posts), 2)

    def test_offline_provider_does_not_block_healthy_provider_or_other_pr(self):
        daemon = self.daemon
        daemon.bitbucket.prs.append(replace(daemon.bitbucket.prs[0], pr_id=14))
        daemon.state.mark_provider_cooldown("codex", "offline", 7200, "quota_exhausted")
        daemon.poll_once()
        daemon.run_pending_jobs()
        rounds = daemon.state.inline.list_rounds()
        for round_ in rounds:
            outcomes = {item["provider"]: item for item in round_["outcomes"]}
            self.assertEqual(outcomes["claude"]["status"], "succeeded")
            self.assertEqual(outcomes["codex"]["status"], "pending")
            self.assertIsNotNone(outcomes["codex"]["recovery_deadline_at"])
        with daemon.state.connect() as conn:
            conn.execute("update inline_round_providers set recovery_deadline_at='2000-01-01T00:00:00+00:00' where provider='codex'")
        daemon.run_pending_jobs()
        self.assertEqual([r["status"] for r in daemon.state.inline.list_rounds()], ["completed", "completed"])
        self.assertEqual(len(daemon.bitbucket.posts), 4)  # Coverage notice and finding for each PR.
        self.assertTrue(any("Codex" in post and "did not" in post for post in daemon.bitbucket.posts))

    def test_healthy_shared_capacity_wait_has_no_failure_clock(self):
        daemon = self.daemon
        daemon.poll_once()
        for provider in daemon.provider_names:
            for _ in range(daemon.provider_configs[provider].max_parallel):
                self.assertTrue(daemon._acquire_provider_slot(provider, blocking=False))
        pool = RecordingPool()
        daemon._schedule(pool, {})
        self.assertEqual(pool.tasks, [])
        self.assertTrue(all(item["recovery_deadline_at"] is None for item in self.round()["outcomes"]))

    def test_permanent_provider_failure_releases_barrier(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("claude")
        with patch.object(daemon.providers["codex"], "run", side_effect=ProviderError("authentication failed", retryable=False)):
            self.run_provider("codex")
        self.assertEqual(self.round()["status"], "ready_for_selection")
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 2)

    def test_selector_cooldown_expires_to_exact_fallback_without_slot(self):
        daemon = self.daemon
        self.enable_selection()
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        daemon.state.mark_provider_cooldown("codex", "offline", 7200, "quota_exhausted")
        daemon.run_pending_jobs()
        self.assertIsNotNone(self.round()["selection_recovery_deadline_at"])
        self.assertEqual(daemon.bitbucket.posts, [])
        with daemon.state.connect() as conn:
            conn.execute("update inline_rounds set selection_recovery_deadline_at='2000-01-01T00:00:00+00:00'")
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 1)

    def test_model_plan_selects_broader_original_comment(self):
        daemon = self.daemon
        self.enable_selection()
        broad = asdict(valid_review())
        broad["annotations"][0]["details"] += " Timeout and authentication failures both leave the state invalid."
        daemon.providers["claude"].final_message = broad
        daemon.providers["codex"].classify_findings = lambda **kwargs: json.dumps({"decisions": [
            {"candidate_id": "codex:finding-001", "decision": "covered", "covered_by": "claude:finding-001", "relationship": "representative_subsumes_candidate", "reason": "Both conditions covered"},
            {"candidate_id": "claude:finding-001", "decision": "retain", "covered_by": None, "relationship": None, "reason": "Full scope"},
        ], "historical_supersessions": []})
        daemon.poll_once()
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 1)
        self.assertIn("Timeout and authentication", daemon.bitbucket.posts[0])

    def test_source_push_replaces_reviewing_round_with_frozen_snapshot(self):
        daemon = self.daemon
        daemon.poll_once()
        original = self.round()
        self.run_provider("codex")
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40)
        daemon.poll_once()
        self.assertNotEqual(original["id"], self.round()["id"])
        self.assertTrue(all(item["status"] == "pending" for item in self.round()["outcomes"]))
        self.assertEqual(self.round()["merge_base_hash"], "b" * 40)
        self.assertEqual(daemon.state.inline.get_round(original["id"])["status"], "superseded")

    def test_source_push_after_barrier_posts_stale_notice_without_replacement(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        original = self.round()["id"]
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40)
        daemon.poll_once()
        self.assertEqual(original, self.round()["id"])
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed_with_stale_findings")
        self.assertEqual(len(daemon.bitbucket.posts), 1)
        self.assertIn("not posted", daemon.bitbucket.posts[0])

    def test_explicit_request_is_atomic_and_processed_once(self):
        daemon = self.daemon
        daemon.poll_once()
        daemon.bitbucket.comments.append({"id": 20, "updated_on": "today", "content": {"raw": "@scout review"}})
        daemon.poll_once()
        requested = self.round()["id"]
        daemon.poll_once()
        self.assertEqual(self.round()["id"], requested)
        self.assertEqual(len(daemon.state.inline.list_rounds()), 2)
        self.assertTrue(daemon.state.processed_pull_request_comment_review_requested("ws", "repo", 13, "20", "today"))

    def test_non_request_mention_does_not_create_round(self):
        daemon = self.daemon
        daemon.providers["codex"].review_requested = False
        daemon.bitbucket.comments.append({"id": 20, "updated_on": "today", "content": {"raw": "@scout thanks"}})
        daemon.poll_once()
        daemon.poll_once()
        self.assertEqual(len(daemon.state.inline.list_rounds()), 1)
        self.assertFalse(daemon.state.processed_pull_request_comment_review_requested("ws", "repo", 13, "20", "today"))

    def test_draft_cancels_round_without_pruning_open_pr_history(self):
        daemon = self.daemon
        daemon.poll_once()
        daemon.state.inline.upsert_history("ws", "repo", 13, "7", {"annotation": {}})
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], is_draft=True)
        daemon.poll_once()
        self.assertEqual(self.round()["status"], "cancelled")
        self.assertEqual(len(daemon.state.inline.history("ws", "repo", 13)), 1)

    def test_startup_unavailable_provider_does_not_abort_inline_reviews(self):
        daemon = self.daemon
        for runner in daemon.providers.values():
            runner.validate_startup = lambda: None
        daemon.providers["codex"].validate_startup = lambda: (_ for _ in ()).throw(ProviderError("offline"))
        daemon.validate_startup()
        self.assertIsNotNone(daemon.state.get_active_provider_cooldown("codex"))
        daemon.poll_once()
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["outcomes"][1]["status"], "succeeded")
