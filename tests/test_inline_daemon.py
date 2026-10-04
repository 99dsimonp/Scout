import json
import tempfile
import threading
import unittest
from concurrent.futures import Future
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scout.bitbucket import BitbucketError
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

    def get_pull_request(self, repo, pr_id, before_request=None):
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
            # Claude can review, so the round does not wait out Codex's cooldown.
            self.assertEqual(outcomes["codex"]["status"], "failed")
        self.assertEqual([r["status"] for r in rounds], ["completed", "completed"])
        self.assertEqual(len(daemon.bitbucket.posts), 4)  # Coverage notice and finding for each PR.
        self.assertTrue(any("Codex" in post and "did not" in post for post in daemon.bitbucket.posts))

    def test_last_provider_able_to_review_waits_out_cooldown(self):
        daemon = self.daemon
        daemon.poll_once()
        for provider in ("codex", "claude"):
            daemon.state.mark_provider_cooldown(provider, "offline", 7200, "quota_exhausted")
        daemon._inline_dispatch().maintain()
        outcomes = self.round()["outcomes"]
        self.assertEqual([item["status"] for item in outcomes], ["pending", "pending"])
        self.assertTrue(all(item["recovery_deadline_at"] for item in outcomes))

    def test_disabled_provider_is_not_a_cooldown_alternative_in_either_order(self):
        daemon = self.daemon
        daemon.state.mark_provider_cooldown("codex", "offline", 7200, "quota_exhausted")
        daemon.provider_names = ["codex"]
        for pr_id, providers in enumerate((("codex", "claude"), ("claude", "codex")), 13):
            with self.subTest(providers=providers):
                pr = replace(daemon.bitbucket.prs[0], pr_id=pr_id, merge_base_hash="b" * 40)
                if pr_id != daemon.bitbucket.prs[0].pr_id:
                    daemon.bitbucket.prs.append(pr)
                record = daemon.state.inline.create_round(pr, providers, "v1", "v1")
                daemon._inline_dispatch().maintain()
                round_ = daemon.state.inline.get_round(record["id"])
                outcomes = {item["provider"]: item for item in round_["outcomes"]}
                self.assertEqual(round_["status"], "reviewing")
                self.assertEqual(outcomes["claude"]["status"], "failed")
                self.assertEqual(outcomes["codex"]["status"], "pending")
                deadline = outcomes["codex"]["recovery_deadline_at"]
                self.assertIsNotNone(deadline)
                with patch("scout.inline_state.utcnow", return_value=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()):
                    daemon._inline_dispatch().maintain()
                outcomes = {item["provider"]: item for item in daemon.state.inline.get_round(record["id"])["outcomes"]}
                self.assertEqual(outcomes["codex"]["recovery_deadline_at"], deadline)
        with daemon.state.connect() as conn:
            conn.execute("delete from provider_state")
        daemon.run_pending_jobs()
        self.assertEqual([item["status"] for item in daemon.state.inline.list_rounds()], ["completed", "completed"])
        self.assertEqual(len(daemon.providers["codex"].runs), 2)
        self.assertEqual(daemon.providers["claude"].runs, [])

    def test_cooldown_error_drops_provider_while_another_can_review(self):
        daemon = self.daemon
        daemon.poll_once()
        daemon.providers["codex"].run = lambda **kwargs: (_ for _ in ()).throw(
            ProviderError("quota", cooldown_seconds=7200, provider_status="quota_exhausted"))
        job = self.daemon.state.claim_next_pending_job({"codex": 1200})
        job = replace(job, attempts=daemon.config.queue.max_attempts)
        daemon.run_job(job)
        self.assertEqual(self.round()["outcomes"][0]["status"], "failed")
        self.run_provider("claude")
        self.assertEqual(self.round()["status"], "ready_for_selection")

    def test_cooldown_error_on_last_attempt_defers_last_provider(self):
        daemon = self.daemon
        daemon.poll_once()
        self.assertTrue(daemon.state.inline.fail_provider(self.round()["id"], "claude", "unavailable"))
        daemon.providers["codex"].run = lambda **kwargs: (_ for _ in ()).throw(
            ProviderError("quota", cooldown_seconds=7200, provider_status="quota_exhausted"))
        job = self.daemon.state.claim_next_pending_job({"codex": 1200})
        job = replace(job, attempts=daemon.config.queue.max_attempts)
        daemon.run_job(job)
        codex = self.round()["outcomes"][0]
        self.assertEqual(codex["status"], "pending")
        self.assertIsNotNone(codex["recovery_deadline_at"])

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

    def test_push_during_review_finishes_original_snapshot_without_retrigger(self):
        daemon = self.daemon
        daemon.poll_once()
        original = self.round()
        self.run_provider("codex")
        pending = daemon.state.claim_next_pending_job({"claude": 1200})
        run = daemon.providers["claude"].run

        def push_during_provider(**kwargs):
            daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40,
                                             destination_commit_hash="f" * 40, merge_base_hash="c" * 40)
            daemon.poll_once()
            self.assertFalse(kwargs["is_superseded"]())
            return run(**kwargs)

        with patch.object(daemon.providers["claude"], "run", side_effect=push_during_provider), \
                patch.object(daemon.git, "resolve_review_snapshot", side_effect=AssertionError("push must not resolve a new snapshot")):
            daemon.run_job(pending)
            daemon.run_pending_jobs()
            for revision in ("7", "8"):
                daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash=revision * 40)
                daemon.poll_once()
                daemon.run_pending_jobs()
        self.assertEqual(len(daemon.state.inline.list_rounds()), 1)
        self.assertEqual(self.round()["id"], original["id"])
        self.assertEqual(self.round()["source_commit_hash"], original["source_commit_hash"])
        self.assertEqual(self.round()["merge_base_hash"], original["merge_base_hash"])
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 2)
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [1, 1])
        self.assertEqual(daemon.state.get_job(pending.id).running_source_commit_hash, original["source_commit_hash"])

    def test_push_after_barrier_publishes_original_findings_without_replacement(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        original = self.round()["id"]
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40,
                                         destination_commit_hash="f" * 40, merge_base_hash="c" * 40)
        daemon.poll_once()
        self.assertEqual(original, self.round()["id"])
        with patch.object(daemon.git, "ensure_mirror", side_effect=AssertionError("publication must not fetch a new snapshot")):
            daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 2)
        self.assertTrue(all("What I found:" in post for post in daemon.bitbucket.posts))

    def test_push_during_selection_preserves_results_and_selected_text(self):
        daemon = self.daemon
        self.enable_selection()
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        original = self.round()

        def select(**kwargs):
            daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40,
                                             destination_commit_hash="f" * 40, merge_base_hash="c" * 40)
            daemon.poll_once()
            return json.dumps({"decisions": [
                {"candidate_id": "codex:finding-001", "decision": "retain", "covered_by": None, "relationship": None, "reason": "Original finding"},
                {"candidate_id": "claude:finding-001", "decision": "covered", "covered_by": "codex:finding-001", "relationship": "equivalent", "reason": "Same finding"},
            ], "historical_supersessions": []})

        with patch.object(daemon.providers["codex"], "classify_findings", create=True, side_effect=select), \
                patch.object(daemon.git, "ensure_mirror", side_effect=AssertionError("selection must keep original snapshot")):
            daemon.run_pending_jobs()
        self.assertEqual(self.round()["id"], original["id"])
        self.assertEqual(self.round()["outcomes"], original["outcomes"])
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 1)
        self.assertIn("found by Codex", daemon.bitbucket.posts[0])
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [1, 1])

    def test_explicit_request_is_atomic_and_processed_once(self):
        daemon = self.daemon
        daemon.poll_once()
        original = self.round()["id"]
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40)
        daemon.bitbucket.comments.append({"id": 20, "updated_on": "today", "content": {"raw": "@scout review"}})
        daemon.poll_once()
        requested = self.round()["id"]
        daemon.poll_once()
        self.assertEqual(self.round()["id"], requested)
        self.assertEqual(len(daemon.state.inline.list_rounds()), 2)
        self.assertEqual(daemon.state.inline.get_round(original)["status"], "superseded")
        self.assertEqual(self.round()["source_commit_hash"], "e" * 40)
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

    def test_closed_inventory_reconciles_without_resend_and_prunes_history(self):
        daemon = self.daemon
        daemon.poll_once()
        daemon.run_pending_jobs()
        before = list(daemon.bitbucket.posts)
        daemon.bitbucket.prs = []
        daemon.poll_once()
        self.assertEqual(daemon.bitbucket.posts, before)
        self.assertEqual(daemon.state.inline.list_rounds(), [])
        self.assertEqual(daemon.state.inline.history("ws", "repo", 13), [])

    def test_closed_pr_fences_active_review_but_defers_pruning_until_worker_exit(self):
        daemon = self.daemon
        daemon.poll_once()
        pool, futures = RecordingPool(), {}
        daemon._schedule(pool, futures)
        round_id = self.round()["id"]
        daemon.bitbucket.prs = []
        daemon.poll_once()
        self.assertEqual(daemon.state.inline.get_round(round_id)["status"], "cancelled")
        for function, args in pool.tasks:
            function(*args)
        self.assertEqual([len(provider.runs) for provider in daemon.providers.values()], [0, 0])
        for future in futures:
            future.set_result(None)
        daemon.poll_once()
        self.assertEqual(daemon.state.inline.list_rounds(), [])

    def test_closed_pr_fences_active_publisher_before_lock_can_be_taken(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        dispatcher = daemon._inline_dispatch()
        lock = threading.Lock()
        dispatcher.locks[("ws", "repo", 13)] = lock
        lock.acquire()
        daemon.bitbucket.prs = []
        daemon.poll_once()
        self.assertEqual(self.round()["status"], "cancelled")
        self.assertEqual(daemon.bitbucket.posts, [])
        lock.release()
        daemon.poll_once()
        self.assertEqual(daemon.state.inline.list_rounds(), [])

    def test_merged_pr_cancels_publication_before_next_poll(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], state="MERGED")
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "cancelled")
        self.assertEqual(daemon.bitbucket.posts, [])

    def test_destination_movement_with_same_merge_base_keeps_inline_findings(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], destination_commit_hash="e" * 40)
        with patch.object(daemon.git, "ensure_mirror", side_effect=AssertionError("publication must not resolve the new base")):
            daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 2)

    def test_publication_retry_uses_saved_results_and_plan(self):
        daemon = self.daemon
        daemon.poll_once()
        daemon.bitbucket.fail = True
        daemon.run_pending_jobs()
        plan = daemon.state.inline.get_plan(self.round()["id"])
        self.assertIsNotNone(plan)
        self.assertEqual(len(daemon.bitbucket.posts), 1)
        daemon.bitbucket.fail = False
        with daemon.state.connect() as conn:
            conn.execute("update inline_rounds set retry_after=null")
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 2)
        self.assertEqual(daemon.state.inline.get_plan(self.round()["id"]), plan)
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [1, 1])

    def test_inline_round_metadata_failure_retries_without_publishing_or_regeneration(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        record = self.round()
        with patch.object(daemon.bitbucket, "get_pull_request", side_effect=BitbucketError("metadata unavailable", retryable=True)):
            daemon.run_pending_jobs()
        self.assertEqual(daemon.bitbucket.posts, [])
        self.assertEqual(self.round()["status"], "publishing")
        self.assertIsNotNone(self.round()["retry_after"])
        self.assertEqual(self.round()["outcomes"], record["outcomes"])
        plan = daemon.state.inline.get_plan(record["id"])
        self.assertIsNotNone(plan)
        intents = daemon.state.inline.list_intents(round_id=record["id"])
        self.assertEqual(len(intents), 2)
        self.assertTrue(all(intent["status"] == "ready" and intent["attempts"] == 0 for intent in intents))
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [1, 1])
        with daemon.state.connect() as conn:
            conn.execute("update inline_rounds set retry_after=null where id=?", (record["id"],))
        daemon.run_pending_jobs()
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(sorted(daemon.bitbucket.posts), sorted(intent["payload"]["content"] for intent in intents))
        self.assertEqual(self.round()["outcomes"], record["outcomes"])
        self.assertEqual(daemon.state.inline.get_plan(record["id"]), plan)
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [1, 1])

    def test_exhausted_unknown_publication_fails_and_operator_can_recover(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        daemon.bitbucket.fail = True
        for attempt in range(daemon.config.queue.publication_max_attempts):
            with daemon.state.connect() as conn:
                conn.execute("update inline_rounds set retry_after=null")
                conn.execute("update inline_publication_intents set last_attempt_at='2000-01-01T00:00:00+00:00'")
            daemon.bitbucket.comments.clear()
            daemon.run_pending_jobs()
            self.assertEqual(len(daemon.bitbucket.posts), attempt + 1)
        plan = daemon.state.inline.get_plan(self.round()["id"])
        with daemon.state.connect() as conn:
            conn.execute("update inline_rounds set retry_after=null")
        daemon.bitbucket.comments.clear()
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "publication_failed")
        unresolved = daemon.state.inline.list_unresolved_publications()
        self.assertEqual(len(unresolved["rounds"]), 1)
        self.assertEqual(len(unresolved["intents"]), 1)
        intent = unresolved["intents"][0]
        self.assertEqual(intent["status"], "unknown")
        self.assertTrue(intent["uncertain"])
        self.assertEqual(intent["attempts"], daemon.config.queue.publication_max_attempts)
        with patch.object(daemon.bitbucket, "list_pull_request_comments", side_effect=AssertionError("terminal round must stop polling")):
            daemon.run_pending_jobs()
        self.assertTrue(daemon.state.inline.resolve_publication(intent["id"], intent["version"], "absent"))
        self.assertTrue(daemon.state.inline.retry_publication(self.round()["id"]))
        daemon.bitbucket.fail = False
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(daemon.state.inline.get_plan(self.round()["id"]), plan)
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [1, 1])

    def test_permanent_selector_error_falls_back_to_exact_plan(self):
        daemon = self.daemon
        self.enable_selection()
        daemon.providers["codex"].classify_findings = lambda **kwargs: (_ for _ in ()).throw(ProviderError("bad credentials", retryable=False))
        daemon.poll_once()
        daemon.run_pending_jobs()
        with daemon.state.connect() as conn:
            conn.execute("update inline_rounds set retry_after=null")
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 1)

    def test_ready_publication_and_reviews_share_workers_fairly(self):
        daemon = self.daemon
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        daemon.bitbucket.prs.append(replace(daemon.bitbucket.prs[0], pr_id=14))
        daemon.poll_once()
        pool, futures = RecordingPool(), {}
        daemon._schedule(pool, futures)
        self.assertEqual(len(pool.tasks), 2)
        self.assertEqual(sum("round_id" in metadata for metadata in futures.values()), 1)
        self.assertEqual(sum(metadata.get("id") is not None for metadata in futures.values()), 1)
        # Run the recorded tasks so their reservations and PR lock are released.
        for function, args in pool.tasks:
            function(*args)

    def test_finished_selector_does_not_keep_its_provider_slot_during_http(self):
        daemon = self.daemon
        daemon.provider_configs["codex"] = replace(daemon.provider_configs["codex"], max_parallel=1)
        daemon.poll_once()
        self.run_provider("codex")
        self.run_provider("claude")
        publishing_id = self.round()["id"]
        daemon.bitbucket.prs.append(replace(daemon.bitbucket.prs[0], pr_id=14))
        daemon.poll_once()
        # Selection has released its reservation, but the publication worker is
        # still using the other global worker for HTTP delivery.
        futures = {Future(): {"round_id": publishing_id, "provider": "codex", "id": None}}
        pool = RecordingPool()
        daemon._schedule(pool, futures)
        self.assertEqual(len(pool.tasks), 1)
        function, args = pool.tasks[0]
        self.assertEqual(args[0].provider, "codex")
        self.assertEqual(args[0].pr_id, 14)
        function(*args)

    def save_legacy_snapshot(self):
        daemon = self.daemon
        pr = daemon.bitbucket.prs[0]
        daemon.state.enqueue_or_update_pr(pr, "v1", "v1", "codex", output_mode="inline_comments")
        job = daemon.state.claim_next_pending_job({"codex": 1200})
        payload = {"version": 1, "source_commit": pr.source_commit_hash,
                   "review": asdict(valid_review()), "review_log": {},
                   "publication": {"report_id": "inline-comments", "destination_commit": pr.destination_commit_hash,
                                   "no_findings_comment": None, "outdated_no_findings_comment": None,
                                   "comments": [{"external_id": "finding-001", "path": "src/app.py", "line": 12,
                                                 "line_side": "NEW", "content": "Immutable saved comment", "outdated_content": "Immutable outdated comment"}]}}
        self.assertTrue(daemon.state.save_review_snapshot(job, payload))
        daemon.state.mark_retryable_failure(job.id, "delivery interrupted", 3, job.lease_token, job.running_review_key, 0)
        return job

    def test_old_inline_saved_snapshot_replays_without_provider_even_in_cooldown(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        daemon.state.mark_provider_cooldown("codex", "unavailable", 7200, "quota_exhausted")
        daemon.poll_once()
        daemon.run_pending_jobs()
        self.assertEqual(daemon.state.get_job(job.id).status, "succeeded")
        self.assertEqual(daemon.bitbucket.posts, ["Immutable saved comment"])
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [0, 0])

    def test_explicit_request_cancels_pending_legacy_replay_from_removed_provider(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        daemon.state.mark_inline_comment_published(job, "already-posted")
        daemon.provider_names = ["claude"]
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40)
        daemon.bitbucket.comments.append({"id": 20, "updated_on": "today", "content": {"raw": "@scout review"}})
        daemon.poll_once()
        self.assertEqual(daemon.state.get_job(job.id).status, "cancelled")
        self.assertIsNone(daemon.state.load_review_snapshot(job))
        self.assertTrue(daemon.state.inline_comment_published(job, "already-posted"))
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(self.round()["providers"], ["claude"])
        self.assertEqual(len(daemon.bitbucket.posts), 1)
        self.assertNotIn("Immutable saved comment", daemon.bitbucket.posts)
        self.assertEqual(daemon.providers["codex"].runs, [])

    def test_explicit_request_fences_legacy_worker_after_snapshot_is_loaded(self):
        daemon = self.daemon
        saved = self.save_legacy_snapshot()
        job = daemon.state.claim_saved_publication_job(1200)
        daemon.provider_names = ["claude"]
        current_pr = daemon.bitbucket.get_pull_request

        def request_during_replay(*args, **kwargs):
            self.assertEqual(daemon.state.get_job(job.id).status, "publishing")
            daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40)
            daemon.bitbucket.comments.append({"id": 20, "updated_on": "today", "content": {"raw": "@scout review"}})
            daemon.poll_once()
            return current_pr(*args, **kwargs)

        with patch.object(daemon.bitbucket, "get_pull_request", side_effect=request_during_replay):
            daemon.run_job(job)
        self.assertEqual(daemon.bitbucket.posts, [])
        self.assertEqual(daemon.state.get_job(saved.id).status, "cancelled")
        self.assertTrue(daemon.state.is_job_superseded(job.id, job.lease_token))
        daemon.run_pending_jobs()
        self.assertEqual(self.round()["status"], "completed")
        self.assertEqual(len(daemon.bitbucket.posts), 1)

    def test_old_inline_snapshot_replays_original_locations_after_push(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], source_commit_hash="e" * 40,
                                         destination_commit_hash="f" * 40)
        with patch.object(daemon.bitbucket, "publish_inline_pull_request_comment", wraps=daemon.bitbucket.publish_inline_pull_request_comment) as publish, \
                patch.object(daemon.bitbucket, "publish_pull_request_comment", side_effect=AssertionError("finding must stay inline")), \
                patch.object(daemon.git, "ensure_mirror", side_effect=AssertionError("replay must not fetch")):
            daemon.poll_once()
            daemon.run_pending_jobs()
        self.assertEqual(daemon.state.get_job(job.id).status, "succeeded")
        self.assertEqual(daemon.bitbucket.posts, ["Immutable saved comment"])
        self.assertEqual(publish.call_args.args, ("repo", 13, "src/app.py", 12, "Immutable saved comment"))
        self.assertEqual(publish.call_args.kwargs["line_side"], "NEW")

    def test_old_inline_snapshot_replay_stops_when_pr_is_no_longer_eligible(self):
        daemon = self.daemon
        pr = daemon.bitbucket.prs[0]
        repo = daemon.repository_configs["repo"]
        for pr_id, (pr_changes, repo_changes) in enumerate((
            ({"state": "MERGED"}, {}),
            ({"is_draft": True}, {}),
            ({}, {"review_enabled": False}),
            ({}, {"ignored_source_branches": ("feature",)}),
            ({}, {"ignored_target_branches": ("main",)}),
        ), 20):
            with self.subTest(pr_changes=pr_changes, repo_changes=repo_changes):
                daemon.bitbucket.prs[0] = replace(pr, pr_id=pr_id)
                job = self.save_legacy_snapshot()
                daemon.bitbucket.prs[0] = replace(daemon.bitbucket.prs[0], **pr_changes)
                daemon.repository_configs["repo"] = replace(repo, **repo_changes)
                daemon.run_pending_jobs()
                self.assertEqual(daemon.state.get_job(job.id).status, "cancelled")
                self.assertEqual(daemon.bitbucket.posts, [])

    def test_old_inline_saved_snapshot_replays_after_provider_is_removed(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        daemon.config = replace(daemon.config, queue=replace(daemon.config.queue, job_timeout_seconds=1))
        del daemon.provider_configs["codex"]
        del daemon.providers["codex"]
        daemon.provider_names = ["claude"]
        with patch.object(daemon, "_acquire_provider_slot", side_effect=AssertionError("replay must not reserve a provider")), \
                patch.object(daemon.git, "ensure_mirror", side_effect=AssertionError("replay must not fetch")):
            pool, futures = RecordingPool(), {}
            self.assertTrue(daemon._schedule_saved_publication(pool, futures))
            for function, args in pool.tasks:
                function(*args)
        self.assertEqual(daemon.state.get_job(job.id).status, "succeeded")
        self.assertEqual(daemon.bitbucket.posts, ["Immutable saved comment"])

    def test_old_inline_snapshot_delivery_failure_retries_frozen_payload(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        with patch.object(daemon.bitbucket, "publish_inline_pull_request_comment", side_effect=BitbucketError("unavailable", retryable=True)):
            daemon.run_pending_jobs()
        self.assertEqual(daemon.state.get_job(job.id).status, "failed_retryable")
        self.assertEqual(daemon.bitbucket.posts, [])
        with daemon.state.connect() as conn:
            conn.execute("update review_jobs set leased_until=null where id=?", (job.id,))
        daemon.run_pending_jobs()
        self.assertEqual(daemon.state.get_job(job.id).status, "succeeded")
        self.assertEqual(daemon.bitbucket.posts, ["Immutable saved comment"])
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [0, 0])

    def test_inline_replay_metadata_failure_retries_without_publishing_or_regeneration(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        snapshot = daemon.state.load_review_snapshot(job)
        self.assertIsNotNone(snapshot)
        with patch.object(daemon.bitbucket, "get_pull_request", side_effect=BitbucketError("metadata unavailable", retryable=True)):
            daemon.run_pending_jobs()
        self.assertEqual(daemon.bitbucket.posts, [])
        self.assertEqual(daemon.state.get_job(job.id).status, "failed_retryable")
        self.assertEqual(daemon.state.load_review_snapshot(job), snapshot)
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [0, 0])
        with daemon.state.connect() as conn:
            conn.execute("update review_jobs set leased_until=null where id=?", (job.id,))
        daemon.run_pending_jobs()
        daemon.run_pending_jobs()
        self.assertEqual(daemon.state.get_job(job.id).status, "succeeded")
        self.assertEqual(daemon.bitbucket.posts, ["Immutable saved comment"])
        self.assertEqual(daemon.state.load_review_snapshot(job), snapshot)
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [0, 0])

    def test_old_inline_saved_snapshot_does_not_reserve_review_capacity(self):
        daemon = self.daemon
        job = self.save_legacy_snapshot()
        daemon.state.mark_provider_cooldown("codex", "unavailable", 7200, "quota_exhausted")
        daemon.poll_once()
        self.assertEqual(daemon.state.inline.list_rounds(), [])
        # With reviews preferred and no cooldown, replay still belongs to the
        # publication class and must not reserve the model provider.
        with daemon.state.connect() as conn:
            conn.execute("delete from provider_state")
        daemon._prefer_publication = False
        pool, futures = RecordingPool(), {}
        daemon._schedule(pool, futures)
        self.assertEqual(len(pool.tasks), 1)
        self.assertEqual(next(iter(futures.values()))["provider"], "")
        for _ in range(daemon.provider_configs["codex"].max_parallel):
            self.assertTrue(daemon._acquire_provider_slot("codex", blocking=False))
        for function, args in pool.tasks:
            function(*args)
        self.assertEqual(daemon.state.get_job(job.id).status, "succeeded")
        self.assertEqual(daemon.bitbucket.posts, ["Immutable saved comment"])
        self.assertEqual([len(runner.runs) for runner in daemon.providers.values()], [0, 0])

    def test_old_pending_inline_job_without_round_does_not_block_scheduling(self):
        daemon = self.daemon
        old_pr = daemon.bitbucket.prs[0]
        for provider in ("codex", "claude"):
            daemon.state.enqueue_or_update_pr(old_pr, "v1", "v1", provider, output_mode="inline_comments")
        with daemon.state.connect() as conn:
            conn.execute("update review_jobs set status='succeeded' where provider='claude'")
        daemon.bitbucket.prs.append(replace(old_pr, pr_id=14))
        daemon.poll_once()
        self.assertEqual([r["pr_id"] for r in daemon.state.inline.list_rounds()], [14])
        pool = RecordingPool()
        daemon._schedule(pool, {})
        self.assertEqual({args[0].pr_id for _, args in pool.tasks}, {14})

    def test_poll_skips_fetch_for_pr_reviewed_before_rounds(self):
        daemon = self.daemon
        daemon.state.enqueue_or_update_pr(daemon.bitbucket.prs[0], "v1", "v1", "codex", output_mode="inline_comments")
        with daemon.state.connect() as conn:
            conn.execute("update review_jobs set status='succeeded'")
        fetches = []
        daemon.git.ensure_mirror = lambda *args: fetches.append(args) or "/mirror"
        daemon.poll_once()
        self.assertEqual(fetches, [])
        self.assertEqual(daemon.state.inline.list_rounds(), [])

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
