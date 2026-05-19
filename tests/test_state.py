import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

import scout.state as state_module
from scout.models import PullRequest
from scout.state import StateStore


class StateTests(unittest.TestCase):
    def test_queue_collapses_new_commits_for_same_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            self.assertTrue(store.enqueue_or_update_pr(pr1, "v1", "v1", "codex"))
            self.assertTrue(store.enqueue_or_update_pr(pr2, "v1", "v1", "codex"))
            jobs = store.claim_pending_jobs(10, 1200)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].running_source_commit_hash, "b" * 40)

    def test_running_job_is_marked_superseded_by_new_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            job = store.claim_pending_jobs(1, 1200)[0]
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")
            self.assertTrue(store.is_job_superseded(job.id))
            store.return_superseded_to_pending(job.id)
            latest = store.claim_pending_jobs(1, 1200)[0]
            self.assertEqual(latest.running_source_commit_hash, "b" * 40)

    def test_running_job_is_not_superseded_by_same_commit_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="initial",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr, "v1", "v1", "codex")
            job = store.claim_pending_jobs(1, 1200)[0]

            self.assertFalse(store.enqueue_or_update_pr(pr, "v1", "v1", "codex"))

            current = store.get_job(job.id)
            self.assertIsNotNone(current)
            self.assertEqual(current.status, "running")
            self.assertFalse(current.superseded)
            self.assertFalse(store.is_job_superseded(job.id, job.lease_token))

    def test_same_pr_id_in_multiple_repositories_creates_distinct_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            for repo_slug, commit in [("repo-a", "a" * 40), ("repo-b", "b" * 40)]:
                pr = PullRequest(
                    workspace="ws",
                    repo_slug=repo_slug,
                    pr_id=1,
                    title="PR",
                    description="",
                    source_branch="feature",
                    source_commit_hash=commit,
                    destination_branch="main",
                )
                self.assertTrue(store.enqueue_or_update_pr(pr, "v1", "v1", "codex"))
            jobs = store.claim_pending_jobs(10, 1200)
            self.assertEqual({job.repo_slug for job in jobs}, {"repo-a", "repo-b"})
            self.assertEqual(len(jobs), 2)

    def test_claim_pending_jobs_can_filter_by_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            store.enqueue_or_update_pr(pr, "v1", "v1", "claude")

            jobs = store.claim_pending_jobs(10, 1200, provider="claude")

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].provider, "claude")
            remaining = store.claim_pending_jobs(10, 1200, provider="codex")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].provider, "codex")

    def test_claim_next_pending_job_uses_global_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="PR 2",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            store.enqueue_or_update_pr(pr1, "v1", "v1", "claude")
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")

            first = store.claim_next_pending_job({"codex": 1200, "claude": 1800})
            second = store.claim_next_pending_job({"codex": 1200, "claude": 1800})

            self.assertEqual((first.provider, first.pr_id), ("codex", 1))
            self.assertEqual((second.provider, second.pr_id), ("claude", 1))

    def test_pending_queue_order_uses_created_at_not_poll_metadata_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            older = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="Older",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            newer = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="Newer",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(older, "v1", "v1", "codex")
            store.enqueue_or_update_pr(newer, "v1", "v1", "codex")
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set created_at=?, updated_at=? where pr_id=1",
                    ("2026-01-01T00:00:00+00:00", "2026-01-03T00:00:00+00:00"),
                )
                conn.execute(
                    "update review_jobs set created_at=?, updated_at=? where pr_id=2",
                    ("2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
                )

            job = store.claim_next_pending_job({"codex": 1200})

            self.assertEqual(job.pr_id, 1)

    def test_claim_next_pending_job_skips_provider_in_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            store.enqueue_or_update_pr(pr, "v1", "v1", "claude")
            store.mark_provider_cooldown("codex", "usage limit reached", cooldown_seconds=5 * 60 * 60)

            job = store.claim_next_pending_job({"codex": 1200, "claude": 1800})

            self.assertEqual(job.provider, "claude")

    def test_claim_next_pending_job_does_not_let_cooldown_freeze_other_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="PR 2",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            store.enqueue_or_update_pr(pr2, "v1", "v1", "claude")
            store.mark_provider_cooldown("codex", "usage limit reached", cooldown_seconds=5 * 60 * 60)

            job = store.claim_next_pending_job({"codex": 1200, "claude": 1200})

            self.assertEqual((job.provider, job.pr_id), ("claude", 2))

    def test_claim_next_pending_job_claims_later_pr_when_older_pr_provider_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="PR 2",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "claude")
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")

            claude = store.claim_next_pending_job({"codex": 1200, "claude": 1200})
            self.assertEqual((claude.provider, claude.pr_id), ("claude", 1))

            next_job = store.claim_next_pending_job({"codex": 1200})
            self.assertEqual((next_job.provider, next_job.pr_id), ("codex", 2))

    def test_prune_closed_pull_requests_removes_inactive_closed_pr_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            open_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="Open",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            closed_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="Closed",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(open_pr, "v1", "v1", "codex")
            store.enqueue_or_update_pr(closed_pr, "v1", "v1", "codex")
            store.mark_report_bootstrap_attempted(closed_pr, "v1", "v1", "codex")

            pruned = store.prune_closed_pull_requests("ws", "repo", [1])

            self.assertEqual(pruned, 1)
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("select count(*) from pull_request_state where pr_id=1").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select count(*) from pull_request_state where pr_id=2").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("select count(*) from review_jobs where pr_id=2").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("select count(*) from report_bootstrap_attempts where pr_id=2").fetchone()[0],
                    0,
                )

    def test_prune_closed_pull_requests_keeps_pr_with_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            closed_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="Closed",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(closed_pr, "v1", "v1", "codex")
            store.claim_pending_jobs(1, 1200)

            pruned = store.prune_closed_pull_requests("ws", "repo", [])

            self.assertEqual(pruned, 0)
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("select count(*) from pull_request_state where pr_id=2").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select status from review_jobs where pr_id=2").fetchone()[0],
                    "running",
                )

    def test_prune_ignored_pull_requests_removes_queued_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            ignored_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="Ignored",
                description="",
                source_branch="release/1.0",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            active_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="Active",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(ignored_pr, "v1", "v1", "codex")
            store.mark_report_bootstrap_attempted(ignored_pr, "v1", "v1", "codex")
            store.enqueue_or_update_pr(active_pr, "v1", "v1", "codex")

            pruned = store.prune_ignored_pull_requests("ws", "repo", [1])

            self.assertEqual(pruned, 1)
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("select count(*) from review_jobs where pr_id=1").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("select count(*) from pull_request_state where pr_id=1").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "select count(*) from report_bootstrap_attempts where pr_id=1"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("select count(*) from review_jobs where pr_id=2").fetchone()[0],
                    1,
                )

    def test_prune_ignored_pull_requests_cancels_running_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            ignored_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="Ignored",
                description="",
                source_branch="release/1.0",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(ignored_pr, "v1", "v1", "codex")
            running = store.claim_pending_jobs(1, 1200)[0]

            pruned = store.prune_ignored_pull_requests("ws", "repo", [1])

            self.assertEqual(pruned, 1)
            current = store.get_job(running.id)
            self.assertIsNotNone(current)
            self.assertEqual(current.status, "cancelled")
            self.assertTrue(current.superseded)
            store.return_superseded_to_pending(running.id, running.lease_token)
            current = store.get_job(running.id)
            self.assertIsNotNone(current)
            self.assertEqual(current.status, "cancelled")
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("select count(*) from review_jobs where pr_id=1").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("select count(*) from pull_request_state where pr_id=1").fetchone()[0],
                    0,
                )

    def test_succeeded_provider_job_is_not_reenqueued_after_other_provider_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            store.enqueue_or_update_pr(pr, "v1", "v1", "claude")
            codex = store.claim_pending_jobs(1, 1200, provider="codex")[0]
            self.assertTrue(store.mark_publishing(codex, 1200))
            self.assertTrue(store.mark_success(codex, "scout-codex-v1"))
            claude = store.claim_pending_jobs(1, 1200, provider="claude")[0]
            self.assertTrue(store.mark_publishing(claude, 1200))
            self.assertTrue(store.mark_success(claude, "scout-claude-v1"))

            self.assertFalse(store.enqueue_or_update_pr(pr, "v1", "v1", "codex"))
            self.assertFalse(store.enqueue_or_update_pr(pr, "v1", "v1", "claude"))

            self.assertEqual(store.claim_pending_jobs(10, 1200), [])

    def test_seed_successful_review_records_succeeded_job_without_pending_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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

            self.assertFalse(store.has_review_for_key(pr, "v1", "v1", "codex"))
            self.assertTrue(store.should_bootstrap_report(pr, "v1", "v1", "codex"))
            store.mark_report_bootstrap_attempted(pr, "v1", "v1", "codex", "missing")
            self.assertFalse(store.should_bootstrap_report(pr, "v1", "v1", "codex"))
            store.seed_successful_review(pr, "v1", "v1", "codex", "scout-codex-v1")

            self.assertTrue(store.has_review_for_key(pr, "v1", "v1", "codex"))
            self.assertFalse(store.has_review_for_key(pr, "v1", "v1", "claude"))
            self.assertEqual(store.claim_pending_jobs(10, 1200), [])
            self.assertFalse(store.enqueue_or_update_pr(pr, "v1", "v1", "codex"))

            with store.connect() as conn:
                state = conn.execute(
                    "select last_reviewed_commit_hash, review_status, last_report_id from pull_request_state"
                ).fetchone()
                job = conn.execute(
                    "select status, target_source_commit_hash, running_source_commit_hash from review_jobs"
                ).fetchone()
            self.assertEqual(state["last_reviewed_commit_hash"], "a" * 40)
            self.assertEqual(state["review_status"], "succeeded")
            self.assertEqual(state["last_report_id"], "scout-codex-v1")
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(job["target_source_commit_hash"], "a" * 40)
            self.assertEqual(job["running_source_commit_hash"], "a" * 40)

    def test_new_target_commit_resets_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            job = store.claim_pending_jobs(1, 1200)[0]
            store.mark_retryable_failure(job.id, "temporary", max_attempts=3, lease_token=job.lease_token)

            self.assertTrue(store.enqueue_or_update_pr(pr2, "v1", "v1", "codex"))
            next_job = store.claim_pending_jobs(1, 1200)[0]

            self.assertEqual(next_job.running_source_commit_hash, "b" * 40)
            self.assertEqual(next_job.attempts, 1)

    def test_seed_successful_review_resets_existing_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            store.mark_retryable_failure(job.id, "temporary", max_attempts=3, lease_token=job.lease_token)

            store.seed_successful_review(pr, "v1", "v1", "codex", "scout-codex-v1")
            current = store.get_job(job.id)

            self.assertEqual(current.status, "succeeded")
            self.assertEqual(current.attempts, 0)

    def test_initialize_migrates_legacy_failed_permanent_jobs_to_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            with store.connect() as conn:
                conn.execute(
                    """
                    update review_jobs set
                      status='failed_permanent',
                      leased_until='2099-01-01T00:00:00+00:00',
                      lease_token='stale-token',
                      error_message='legacy permanent failure'
                    where id=?
                    """,
                    (job.id,),
                )

            store.initialize()
            current = store.get_job(job.id)

            self.assertEqual(current.status, "failed_retryable")
            self.assertIsNone(current.leased_until)
            self.assertIsNone(current.lease_token)
            self.assertEqual(current.error_message, "legacy permanent failure")

    def test_concurrent_claims_select_distinct_pending_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            for pr_id, commit in [(1, "a" * 40), (2, "b" * 40)]:
                pr = PullRequest(
                    workspace="ws",
                    repo_slug="repo",
                    pr_id=pr_id,
                    title="PR",
                    description="",
                    source_branch="feature",
                    source_commit_hash=commit,
                    destination_branch="main",
                )
                store.enqueue_or_update_pr(pr, "v1", "v1", "codex")

            barrier = threading.Barrier(3, timeout=5)
            claimed = []
            errors = []
            lock = threading.Lock()
            original_clear = state_module._clear_expired_provider_cooldowns
            state_module._clear_expired_provider_cooldowns = lambda conn, now: None
            try:
                def claim_one():
                    try:
                        barrier.wait()
                        jobs = store.claim_pending_jobs(1, 1200)
                        with lock:
                            claimed.extend(job.id for job in jobs)
                    except Exception as exc:
                        with lock:
                            errors.append(exc)

                threads = [threading.Thread(target=claim_one) for _ in range(2)]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join()
            finally:
                state_module._clear_expired_provider_cooldowns = original_clear

            self.assertEqual(errors, [])
            self.assertEqual(len(claimed), 2)
            self.assertEqual(len(set(claimed)), 2)

    def test_concurrent_claim_next_pending_job_selects_distinct_oldest_pr_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="PR 2",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            store.enqueue_or_update_pr(pr1, "v1", "v1", "claude")
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")

            barrier = threading.Barrier(3, timeout=5)
            claimed = []
            errors = []
            lock = threading.Lock()
            original_clear = state_module._clear_expired_provider_cooldowns
            state_module._clear_expired_provider_cooldowns = lambda conn, now: None
            try:
                def claim_one():
                    try:
                        barrier.wait()
                        job = store.claim_next_pending_job({"codex": 1200, "claude": 1200})
                        with lock:
                            if job is not None:
                                claimed.append((job.provider, job.pr_id))
                    except Exception as exc:
                        with lock:
                            errors.append(exc)

                threads = [threading.Thread(target=claim_one) for _ in range(2)]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join()
            finally:
                state_module._clear_expired_provider_cooldowns = original_clear

            self.assertEqual(errors, [])
            self.assertCountEqual(claimed, [("codex", 1), ("claude", 1)])

    def test_concurrent_enqueue_for_same_pr_keeps_single_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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

            barrier = threading.Barrier(3, timeout=5)
            queued = []
            errors = []
            lock = threading.Lock()

            def enqueue_one():
                try:
                    barrier.wait()
                    result = store.enqueue_or_update_pr(pr, "v1", "v1", "codex")
                    with lock:
                        queued.append(result)
                except Exception as exc:
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=enqueue_one) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            jobs = store.claim_pending_jobs(10, 1200)
            self.assertEqual(errors, [])
            self.assertCountEqual(queued, [True, False])
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].pr_id, 1)

    def test_provider_cooldown_blocks_claims_until_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            cooldown_until = store.mark_provider_cooldown(
                "codex",
                "usage limit reached",
                cooldown_seconds=5 * 60 * 60,
            )

            self.assertEqual(store.claim_pending_jobs(10, 1200, provider="codex"), [])

            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                row = conn.execute(
                    "select status, cooldown_until, last_error from provider_state where provider='codex'"
                ).fetchone()
                self.assertEqual(row["status"], "quota_exhausted")
                self.assertEqual(row["cooldown_until"], cooldown_until)
                self.assertEqual(row["last_error"], "usage limit reached")
                conn.execute(
                    "update provider_state set cooldown_until=? where provider='codex'",
                    (expired,),
                )

            jobs = store.claim_pending_jobs(10, 1200, provider="codex")

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].provider, "codex")
            with store.connect() as conn:
                row = conn.execute(
                    "select status, cooldown_until from provider_state where provider='codex'"
                ).fetchone()
                self.assertEqual(row["status"], "available")
                self.assertIsNone(row["cooldown_until"])

    def test_provider_cooldown_deferral_keeps_job_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            with store.connect() as conn:
                conn.execute("update review_jobs set attempts=3 where id=?", (job.id,))

            self.assertTrue(
                store.defer_job_for_provider_cooldown(
                    job.id,
                    "provider is cooling down",
                    job.lease_token,
                    job.running_review_key,
                )
            )
            current = store.get_job(job.id)

            self.assertEqual(current.status, "failed_retryable")
            self.assertEqual(current.attempts, 2)
            self.assertIsNone(current.leased_until)
            self.assertIsNone(current.lease_token)
            self.assertEqual(current.error_message, "provider is cooling down")

    def test_retryable_failure_stays_retryable_at_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            with store.connect() as conn:
                conn.execute("update review_jobs set attempts=3 where id=?", (job.id,))

            marked = store.mark_retryable_failure(
                job.id,
                "schema invalid",
                max_attempts=3,
                lease_token=job.lease_token,
                running_review_key=job.running_review_key,
            )
            current = store.get_job(job.id)

            self.assertTrue(marked)
            self.assertEqual(current.status, "failed_retryable")
            self.assertEqual(current.attempts, 3)
            self.assertIsNone(current.leased_until)

    def test_infrastructure_retryable_failure_stays_retryable_and_defers_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="PR 2",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")
            failed = store.claim_next_pending_job({"codex": 1200})

            marked = store.mark_retryable_failure(
                failed.id,
                "ssh: Could not resolve hostname bitbucket.org",
                max_attempts=1,
                lease_token=failed.lease_token,
                running_review_key=failed.running_review_key,
                retry_backoff_seconds=60,
            )
            current = store.get_job(failed.id)
            next_job = store.claim_next_pending_job({"codex": 1200})

            self.assertTrue(marked)
            self.assertEqual(current.status, "failed_retryable")
            self.assertIsNotNone(current.leased_until)
            self.assertEqual(next_job.pr_id, 2)

            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, failed.id),
                )
            retried = store.claim_next_pending_job({"codex": 1200})

            self.assertEqual(retried.id, failed.id)
            self.assertEqual(retried.attempts, 2)

    def test_retryable_failure_runs_after_pending_work_created_during_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            failed_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="Failed",
                description="",
                source_branch="feature-1",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            waiting_pr = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="Waiting",
                description="",
                source_branch="feature-2",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(failed_pr, "v1", "v1", "codex")
            failed = store.claim_next_pending_job({"codex": 1200})
            self.assertTrue(
                store.mark_retryable_failure(
                    failed.id,
                    "temporary network failure",
                    max_attempts=3,
                    lease_token=failed.lease_token,
                    running_review_key=failed.running_review_key,
                    retry_backoff_seconds=60,
                )
            )
            store.enqueue_or_update_pr(waiting_pr, "v1", "v1", "codex")
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, failed.id),
                )

            next_job = store.claim_next_pending_job({"codex": 1200})

            self.assertEqual(next_job.pr_id, 2)

    def test_expired_running_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            first = store.claim_pending_jobs(1, 1200)[0]
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, first.id),
                )

            reclaimed = store.claim_pending_jobs(1, 1200)

            self.assertEqual(len(reclaimed), 1)
            self.assertEqual(reclaimed[0].id, first.id)
            self.assertEqual(reclaimed[0].attempts, 2)
            self.assertNotEqual(reclaimed[0].lease_token, first.lease_token)
            self.assertTrue(store.is_job_superseded(first.id, first.lease_token))
            self.assertFalse(store.is_job_superseded(reclaimed[0].id, reclaimed[0].lease_token))

    def test_unexpired_running_lease_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            store.claim_pending_jobs(1, 1200)

            self.assertEqual(store.claim_pending_jobs(1, 1200), [])

    def test_recover_abandoned_jobs_resets_running_and_publishing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr1_updated = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR 1",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=2,
                title="PR 2",
                description="",
                source_branch="feature",
                source_commit_hash="c" * 40,
                destination_branch="main",
            )
            pr3 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=3,
                title="PR 3",
                description="",
                source_branch="feature",
                source_commit_hash="d" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            running = store.claim_pending_jobs(1, 1200)[0]
            store.enqueue_or_update_pr(pr1_updated, "v1", "v1", "codex")
            store.enqueue_or_update_pr(pr2, "v1", "v1", "claude")
            publishing = store.claim_pending_jobs(1, 1200)[0]
            self.assertTrue(store.mark_publishing(publishing, 1200))
            store.enqueue_or_update_pr(pr3, "v1", "v1", "codex")
            running_attempts = store.get_job(running.id).attempts
            publishing_attempts = store.get_job(publishing.id).attempts
            with store.connect() as conn:
                pending_id = conn.execute(
                    "select id from review_jobs where pr_id=3"
                ).fetchone()["id"]

            recovered = store.recover_abandoned_jobs("abandoned after service restart")

            self.assertEqual(recovered, 2)
            recovered_running = store.get_job(running.id)
            recovered_publishing = store.get_job(publishing.id)
            pending = store.claim_pending_jobs(10, 1200)
            self.assertEqual(recovered_running.status, "pending")
            self.assertFalse(recovered_running.superseded)
            self.assertEqual(recovered_running.target_source_commit_hash, "b" * 40)
            self.assertIsNone(recovered_running.running_source_commit_hash)
            self.assertIsNone(recovered_running.running_review_key)
            self.assertIsNone(recovered_running.leased_until)
            self.assertIsNone(recovered_running.lease_token)
            self.assertEqual(recovered_running.attempts, running_attempts)
            self.assertEqual(recovered_running.error_message, "abandoned after service restart")
            self.assertEqual(recovered_publishing.status, "pending")
            self.assertEqual(recovered_publishing.target_source_commit_hash, "c" * 40)
            self.assertIsNone(recovered_publishing.running_source_commit_hash)
            self.assertIsNone(recovered_publishing.running_review_key)
            self.assertIsNone(recovered_publishing.leased_until)
            self.assertIsNone(recovered_publishing.lease_token)
            self.assertEqual(recovered_publishing.attempts, publishing_attempts)
            self.assertEqual(recovered_publishing.error_message, "abandoned after service restart")
            self.assertEqual({job.id for job in pending}, {running.id, publishing.id, pending_id})

    def test_claim_recovered_job_clears_restart_error_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            running = store.claim_pending_jobs(1, 1200)[0]

            recovered = store.recover_abandoned_jobs()
            recovered_job = store.get_job(running.id)
            claimed = store.claim_next_pending_job({"codex": 1200})

            self.assertEqual(recovered, 1)
            self.assertEqual(
                recovered_job.error_message,
                "Scout restarted while this job was active",
            )
            self.assertEqual(claimed.id, running.id)
            self.assertIsNone(claimed.error_message)
            self.assertIsNone(store.get_job(claimed.id).error_message)

    def test_batch_claim_recovered_job_clears_restart_error_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            running = store.claim_pending_jobs(1, 1200)[0]

            recovered = store.recover_abandoned_jobs()
            recovered_job = store.get_job(running.id)
            claimed = store.claim_pending_jobs(1, 1200)[0]

            self.assertEqual(recovered, 1)
            self.assertEqual(
                recovered_job.error_message,
                "Scout restarted while this job was active",
            )
            self.assertEqual(claimed.id, running.id)
            self.assertIsNone(claimed.error_message)
            self.assertIsNone(store.get_job(claimed.id).error_message)

    def test_stale_worker_stays_superseded_after_expired_reclaim_for_new_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            old = store.claim_pending_jobs(1, 1200)[0]
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")
            self.assertFalse(store.mark_publishing(old, 1200))
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, old.id),
                )

            new = store.claim_pending_jobs(1, 1200)[0]

            self.assertEqual(new.running_source_commit_hash, "b" * 40)
            self.assertTrue(store.is_job_superseded(old.id, old.lease_token))
            self.assertFalse(store.mark_publishing(old, 1200))
            self.assertTrue(store.mark_publishing(new, 1200))

    def test_stale_worker_failure_does_not_fail_new_target_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            old = store.claim_pending_jobs(1, 1200)[0]
            store.enqueue_or_update_pr(pr2, "v1", "v1", "codex")

            marked = store.mark_retryable_failure(
                old.id,
                "old failure",
                1,
                old.lease_token,
                old.running_review_key,
            )
            current = store.get_job(old.id)

            self.assertFalse(marked)
            self.assertEqual(current.status, "running")
            self.assertTrue(current.superseded)
            self.assertEqual(current.target_source_commit_hash, "b" * 40)
            store.return_superseded_to_pending(old.id, old.lease_token)
            pending = store.claim_pending_jobs(1, 1200)[0]
            self.assertEqual(pending.running_source_commit_hash, "b" * 40)

    def test_stale_worker_cannot_clear_reclaimed_job_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            old = store.claim_pending_jobs(1, 1200)[0]
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, old.id),
                )
            new = store.claim_pending_jobs(1, 1200)[0]

            self.assertFalse(store.mark_retryable_failure(old.id, "old failure", 3, old.lease_token))
            store.return_superseded_to_pending(old.id, old.lease_token)
            current = store.get_job(new.id)

            self.assertEqual(current.status, "running")
            self.assertEqual(current.lease_token, new.lease_token)
            self.assertIsNone(current.error_message)

    def test_mark_publishing_renews_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            job = store.claim_pending_jobs(1, 1)[0]
            old_lease = job.leased_until

            self.assertTrue(store.mark_publishing(job, 1200))
            publishing = store.get_job(job.id)

            self.assertEqual(publishing.status, "publishing")
            self.assertGreater(publishing.leased_until, old_lease)
            self.assertEqual(store.claim_pending_jobs(1, 1200), [])

    def test_publishing_job_is_not_superseded_by_new_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
            store.initialize()
            pr1 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="a" * 40,
                destination_branch="main",
            )
            pr2 = PullRequest(
                workspace="ws",
                repo_slug="repo",
                pr_id=1,
                title="PR",
                description="",
                source_branch="feature",
                source_commit_hash="b" * 40,
                destination_branch="main",
            )
            store.enqueue_or_update_pr(pr1, "v1", "v1", "codex")
            publishing = store.claim_pending_jobs(1, 1200)[0]
            self.assertTrue(store.mark_publishing(publishing, 1200))

            self.assertTrue(store.enqueue_or_update_pr(pr2, "v1", "v1", "codex"))
            current = store.get_job(publishing.id)

            self.assertEqual(current.status, "publishing")
            self.assertFalse(current.superseded)
            self.assertEqual(current.target_source_commit_hash, "a" * 40)
            self.assertEqual(current.running_source_commit_hash, "a" * 40)
            self.assertTrue(store.renew_publishing_lease(publishing, 1200))
            self.assertTrue(store.mark_success(publishing, "report"))
            self.assertTrue(store.enqueue_or_update_pr(pr2, "v1", "v1", "codex"))
            next_job = store.claim_pending_jobs(1, 1200)[0]
            self.assertEqual(next_job.running_source_commit_hash, "b" * 40)

    def test_stale_publisher_cannot_renew_after_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            publishing = store.claim_pending_jobs(1, 1200)[0]
            self.assertTrue(store.mark_publishing(publishing, 1200))
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, publishing.id),
                )
            reclaimed = store.claim_pending_jobs(1, 1200)[0]

            self.assertFalse(store.renew_publishing_lease(publishing, 1200))
            self.assertTrue(store.is_job_superseded(publishing.id, publishing.lease_token))
            self.assertFalse(store.is_job_superseded(reclaimed.id, reclaimed.lease_token))

    def test_stale_mark_success_after_reclaim_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            publishing = store.claim_pending_jobs(1, 1200)[0]
            self.assertTrue(store.mark_publishing(publishing, 1200))
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, publishing.id),
                )
            reclaimed = store.claim_pending_jobs(1, 1200)[0]

            self.assertFalse(store.mark_success(publishing, "report"))
            current = store.get_job(reclaimed.id)
            self.assertEqual(current.status, "running")
            self.assertEqual(current.lease_token, reclaimed.lease_token)

    def test_expired_publishing_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp + "/state.db")
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
            self.assertTrue(store.mark_publishing(job, 1200))
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0).isoformat()
            with store.connect() as conn:
                conn.execute(
                    "update review_jobs set leased_until=?, updated_at=? where id=?",
                    (expired, expired, job.id),
                )

            reclaimed = store.claim_pending_jobs(1, 1200)

            self.assertEqual(len(reclaimed), 1)
            self.assertEqual(reclaimed[0].status, "running")
            self.assertEqual(reclaimed[0].attempts, 2)


if __name__ == "__main__":
    unittest.main()
