import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from scout.models import PullRequest
from scout.state import StateStore


class InlineStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(self.tmp.name + "/state.db")
        self.store.initialize()
        self.pr = PullRequest("ws", "repo", 1, "PR", "", "feature", "source", "main", "dest", "base")
        self.inline = self.store.inline

    def create(self, providers=("codex", "claude"), **kwargs):
        return self.inline.create_round(self.pr, providers, "v1", "v1", **kwargs)

    def finish(self, provider="codex", findings=None):
        job = self.store.claim_next_pending_job({provider: 120})
        self.assertIsNotNone(job)
        self.assertTrue(self.inline.save_result(job, {"findings": findings or []}))
        return job

    def ready(self):
        round_ = self.create(("codex",))
        self.finish()
        return self.inline.claim_round(round_["id"], 120)

    def test_barrier_retains_success_while_other_provider_fails(self):
        round_ = self.create()
        self.finish()
        self.assertEqual(self.inline.get_round(round_["id"])["status"], "reviewing")
        self.assertTrue(self.inline.fail_provider(round_["id"], "claude", "unavailable"))
        current = self.inline.get_round(round_["id"])
        self.assertEqual(current["status"], "ready_for_selection")
        self.assertEqual([o["status"] for o in current["outcomes"]], ["succeeded", "failed"])
        self.assertIsNone(self.store.claim_next_pending_job({"codex": 120, "claude": 120}))

    def test_first_error_starts_fixed_deadline_and_fences_late_result(self):
        round_ = self.create()
        job = self.store.claim_next_pending_job({"codex": 120})
        self.assertIsNone(self.inline.get_round(round_["id"])["outcomes"][0]["recovery_deadline_at"])
        with patch("scout.inline_state.utcnow", return_value="2026-01-01T00:00:00+00:00"):
            first = self.inline.start_provider_recovery(round_["id"], "codex", 60)
            self.assertEqual(first, self.inline.start_provider_recovery(round_["id"], "codex", 600))
        self.assertFalse(self.inline.save_result(job, {"findings": []}))
        self.assertEqual(self.inline.get_round(round_["id"])["outcomes"][0]["status"], "failed")

    def test_atomic_explicit_request_supersedes_previous_run(self):
        round_ = self.create(request_comment=("1", "today"), trigger="request")
        self.assertTrue(self.store.processed_pull_request_comment_review_requested("ws", "repo", 1, "1", "today"))
        self.assertIsNone(self.create(request_comment=("1", "today"), trigger="request"))
        old_job = self.store.claim_next_pending_job({"codex": 120})
        changed = replace(self.pr, source_commit_hash="new")
        newer = self.inline.create_round(changed, ("codex", "claude"), "v1", "v1", trigger="request", request_comment=("2", "today"))
        self.assertIsNotNone(newer)
        self.assertFalse(self.inline.save_result(old_job, {"findings": []}))
        self.assertEqual(self.inline.get_round(round_["id"])["status"], "superseded")

    def test_explicit_request_supersedes_ready_round(self):
        ready = self.ready()
        newer = self.inline.create_round(replace(self.pr, source_commit_hash="new"), ("codex",), "v1", "v1",
                                        trigger="request", request_comment=("2", "today"))
        self.assertEqual(newer["source_commit_hash"], "new")
        self.assertEqual(self.inline.get_round(ready["id"])["status"], "superseded")
        self.assertFalse(self.inline.save_plan(ready["id"], ready["lease_token"], {}))

    def test_initial_poll_does_not_create_another_round(self):
        self.create()
        self.assertIsNone(self.create())

    def test_all_failed_posts_nothing(self):
        round_ = self.create()
        self.inline.fail_provider(round_["id"], "codex", "permanent")
        self.inline.fail_provider(round_["id"], "claude", "permanent")
        self.assertEqual(self.inline.get_round(round_["id"])["status"], "review_failed")
        self.assertIsNone(self.inline.claim_round(round_["id"], 120))

    def test_expired_selector_cannot_commit_model_plan(self):
        ready = self.ready()
        with patch("scout.inline_state.utcnow", return_value="2026-01-01T00:00:00+00:00"):
            self.inline.start_selection_recovery(ready["id"], 60)
        self.assertFalse(self.inline.save_plan(ready["id"], ready["lease_token"], {"selected": ["a"]}))
        self.assertTrue(self.inline.save_plan(ready["id"], ready["lease_token"], {"selected": ["a", "b"]}, kind="fallback"))
        self.assertFalse(self.inline.save_plan(ready["id"], ready["lease_token"], {"selected": []}, kind="fallback"))
        self.assertEqual(self.inline.get_plan(ready["id"])["selected"], ["a", "b"])

    def test_intent_cas_survives_restart_and_operator_fences_daemon(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {"body": "test"})
        sent = self.inline.transition_intent(intent["id"], intent["version"], "sending")
        unknown = self.inline.transition_intent(sent["id"], sent["version"], "unknown")
        self.store.initialize()
        self.assertTrue(self.inline.resolve_publication(unknown["id"], unknown["version"], "published", "123"))
        self.assertIsNone(self.inline.transition_intent(unknown["id"], unknown["version"], "ready"))
        self.assertEqual(self.inline.get_intent(unknown["id"])["comment_ids"], ["123"])

    def test_absent_superseded_unknown_is_cancelled(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {"body": "test"})
        intent = self.inline.transition_intent(intent["id"], intent["version"], "sending")
        self.create(trigger="request", request_comment=("2", "today"))
        intent = self.inline.get_intent(intent["id"])
        self.assertEqual(intent["status"], "unknown")
        self.assertTrue(self.inline.resolve_publication(intent["id"], intent["version"], "absent"))
        self.assertEqual(self.inline.get_intent(intent["id"])["status"], "cancelled")

    def test_ignored_pr_preserves_history_and_unknown(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {"body": "test"})
        self.inline.transition_intent(intent["id"], intent["version"], "sending")
        self.inline.upsert_history("ws", "repo", 1, "42", {"text": "old"})
        self.store.prune_ignored_pull_requests("ws", "repo", [1])
        self.assertEqual(self.inline.get_round(ready["id"])["status"], "cancelled")
        self.assertEqual(self.inline.get_intent(intent["id"])["status"], "unknown")
        self.assertEqual(len(self.inline.history("ws", "repo", 1)), 1)

    def test_atomic_request_rollback_never_leaves_partial_provider_set(self):
        self.store.enqueue_or_update_pr(self.pr, "old-policy", "v1", "codex", output_mode="inline_comments")
        legacy = self.store.claim_next_pending_job({"codex": 120})
        snapshot = {"publication": {"comments": ["original"]}}
        self.assertTrue(self.store.save_review_snapshot(legacy, snapshot))
        with self.store.connect() as conn:
            conn.execute("""create trigger reject_second_provider before insert on inline_round_providers
                when new.provider='claude' begin select raise(abort,'test interruption'); end""")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(trigger="request", request_comment=("3", "today"))
        self.assertEqual(self.inline.list_rounds(), [])
        self.assertIsNone(self.store.processed_pull_request_comment_review_requested("ws", "repo", 1, "3", "today"))
        self.assertIsNone(self.store.claim_next_pending_job({"codex": 120, "claude": 120}))
        self.assertEqual(self.store.get_job(legacy.id), legacy)
        self.assertEqual(self.store.load_review_snapshot(legacy), snapshot)

    def test_expired_lease_cannot_save_plan_or_send_on_new_lease(self):
        old = self.ready()
        with self.store.connect() as conn:
            conn.execute("update inline_rounds set leased_until='2000-01-01T00:00:00+00:00' where id=?", (old["id"],))
        current = self.inline.claim_round(old["id"], 120)
        self.assertFalse(self.inline.save_plan(old["id"], old["lease_token"], {}))
        self.assertTrue(self.inline.save_plan(current["id"], current["lease_token"], {}))
        intent = self.inline.reserve_intent(current["id"], "a", {})
        self.assertIsNone(self.inline.transition_intent(intent["id"], intent["version"], "sending", lease_token=old["lease_token"]))
        self.assertIsNotNone(self.inline.transition_intent(intent["id"], intent["version"], "sending", lease_token=current["lease_token"]))

    def test_selection_attempts_do_not_consume_delivery_attempts(self):
        ready = self.ready()
        self.inline.start_selection_recovery(ready["id"], 600)
        self.assertTrue(self.inline.selection_failed(ready["id"], ready["lease_token"], "invalid model output", permanent=True))
        ready = self.inline.claim_round(ready["id"], 120)
        self.assertEqual(ready["selection_attempts"], 1)
        self.assertEqual(ready["attempts"], 0)
        self.assertFalse(self.inline.save_plan(ready["id"], ready["lease_token"], {}, kind="model"))
        self.assertTrue(self.inline.save_plan(ready["id"], ready["lease_token"], {}, kind="fallback"))

    def test_retry_resets_known_unsent_budget_and_preserves_plan(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {"retained_ids": ["a"]})
        unsent = self.inline.reserve_intent(ready["id"], "a", {})
        unknown = self.inline.reserve_intent(ready["id"], "b", {})
        unknown = self.inline.transition_intent(unknown["id"], unknown["version"], "sending")
        unknown = self.inline.transition_intent(unknown["id"], unknown["version"], "unknown")
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set attempts=3 where id=?", (unsent["id"],))
        self.inline.release_round(ready["id"], ready["lease_token"], status="publication_failed")
        self.assertTrue(self.inline.retry_publication(ready["id"]))
        self.assertEqual(self.inline.get_intent(unsent["id"])["attempts"], 0)
        self.assertEqual(self.inline.get_intent(unknown["id"])["attempts"], 1)
        self.assertEqual(self.inline.get_plan(ready["id"]), {"retained_ids": ["a"]})

    def test_operator_absence_preserves_finding_and_dependents_for_resend(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {"finding": {"id": "a"}, "covers": ["b"]})
        intent = self.inline.transition_intent(intent["id"], intent["version"], "sending")
        intent = self.inline.transition_intent(intent["id"], intent["version"], "unknown")
        self.assertTrue(self.inline.resolve_publication(intent["id"], intent["version"], "absent"))
        current = self.inline.get_intent(intent["id"])
        self.assertEqual(current["status"], "ready")
        self.assertFalse(current["uncertain"])
        self.assertEqual(current["payload"], intent["payload"])
        self.assertEqual(self.inline.candidate_outcomes(ready["id"]), {})

    def test_shutdown_and_reopen_preserves_result_and_recovery_deadline(self):
        round_ = self.create()
        self.finish()
        deadline = self.inline.start_provider_recovery(round_["id"], "claude", 3600)
        reopened = StateStore(self.store.path)
        reopened.initialize()
        reopened.recover_abandoned_jobs()
        current = reopened.inline.get_round(round_["id"])
        self.assertEqual(current["outcomes"][0]["status"], "succeeded")
        self.assertEqual(current["outcomes"][1]["recovery_deadline_at"], deadline)
        self.assertIsNone(reopened.claim_next_pending_job({"codex": 120}))

    def test_late_duplicate_anomaly_keeps_terminal_status_and_all_ids(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {})
        intent = self.inline.transition_intent(intent["id"], intent["version"], "sending")
        intent = self.inline.transition_intent(intent["id"], intent["version"], "published", comment_ids=[1])
        self.inline.record_anomaly(intent["id"], [2], "late duplicate")
        current = self.inline.get_intent(intent["id"])
        self.assertEqual(current["comment_ids"], ["1", "2"])
        self.assertEqual(current["status"], "published")

    def test_disabled_repository_keeps_unknown_and_history_until_closure(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {})
        self.inline.transition_intent(intent["id"], intent["version"], "sending")
        self.inline.upsert_history("ws", "repo", 1, "1", {}, content="old")
        self.store.upsert_repository("ws", "repo", "git@example", enabled=False)
        self.assertEqual(self.inline.get_intent(intent["id"])["status"], "unknown")
        self.assertEqual(len(self.inline.history("ws", "repo", 1)), 1)
        self.inline.finish_closed_pr("ws", "repo", 1)
        self.assertIsNone(self.inline.get_round(ready["id"]))
        self.assertIsNone(self.inline.get_intent(intent["id"]))
        self.assertEqual(self.inline.history("ws", "repo", 1), [])

    def test_legacy_saved_inline_snapshot_is_not_destroyed_by_initial_round(self):
        self.store.enqueue_or_update_pr(self.pr, "v1", "v1", "codex", output_mode="inline_comments")
        job = self.store.claim_next_pending_job({"codex": 120})
        snapshot = {"version": 1, "source_commit": "source", "review": {"annotations": []},
                    "publication": {"comments": [{"content": "frozen original"}]}}
        self.assertTrue(self.store.save_review_snapshot(job, snapshot))
        self.store.mark_inline_comment_published(job, "already-posted")
        self.assertIsNone(self.create())
        self.assertEqual(self.store.load_review_snapshot(job), snapshot)
        self.assertTrue(self.store.inline_comment_published(job, "already-posted"))
        self.assertEqual(self.store.get_job(job.id).running_review_run_id, job.running_review_run_id)

    def test_explicit_request_cancels_unfinished_legacy_identities_only(self):
        protected = []
        cancelled = []
        for provider, policy, schema, mode, status in (
            ("codex", "v1", "v1", "inline_comments", "failed_retryable"),
            ("claude", "old-policy", "v1", "inline_comments", "running"),
            ("claude", "v1", "old-schema", "inline_comments", "publishing"),
            ("codex", "finished", "v1", "inline_comments", "succeeded"),
            ("codex", "report", "v1", "reports", "running"),
        ):
            self.store.enqueue_or_update_pr(self.pr, policy, schema, provider, output_mode=mode)
            job = self.store.claim_next_pending_job({provider: 120})
            self.assertIsNotNone(job)
            self.assertTrue(self.store.save_review_snapshot(job, {"publication": {"comments": ["original"]}}))
            self.store.mark_inline_comment_published(job, "already-posted")
            # Distinct identities let each legacy status coexist in the same PR.
            with self.store.connect() as conn:
                conn.execute("update review_jobs set status=? where id=?", (status, job.id))
            (protected if status == "succeeded" or mode == "reports" else cancelled).append(job)
        self.inline.upsert_history("ws", "repo", 1, "42", {"text": "published history"})
        history = self.inline.history("ws", "repo", 1)
        self.create(("claude",), trigger="request", request_comment=("new", "today"))
        for job in cancelled:
            with self.subTest(provider=job.provider, policy=job.reviewer_policy_version, schema=job.schema_version):
                current = self.store.get_job(job.id)
                self.assertEqual(current.status, "cancelled")
                self.assertIsNone(current.lease_token)
                self.assertIsNone(current.leased_until)
                self.assertTrue(self.store.is_job_superseded(job.id, job.lease_token))
                self.assertFalse(self.store.renew_job_lease(job, 120))
                self.assertIsNone(self.store.load_review_snapshot(job))
                self.assertTrue(self.store.inline_comment_published(job, "already-posted"))
        for job in protected:
            self.assertEqual(self.store.get_job(job.id).status, "succeeded" if job.output_mode == "inline_comments" else "running")
            self.assertIsNotNone(self.store.load_review_snapshot(job))
            self.assertTrue(self.store.inline_comment_published(job, "already-posted"))
        self.assertEqual(self.inline.history("ws", "repo", 1), history)

    def test_saved_publication_claim_bypasses_cooldown_without_claiming_new_review(self):
        self.store.enqueue_or_update_pr(self.pr, "v1", "v1", "codex", output_mode="inline_comments")
        job = self.store.claim_next_pending_job({"codex": 120})
        self.store.save_review_snapshot(job, {"source_commit": "source", "publication": {}})
        self.store.mark_retryable_failure(job.id, "HTTP unavailable", 3, job.lease_token, job.running_review_key)
        self.store.enqueue_or_update_pr(replace(self.pr, pr_id=2), "v1", "v1", "claude")
        self.store.mark_provider_cooldown("codex", "quota", 3600)
        self.assertIsNone(self.store.claim_next_pending_job({"codex": 120}))
        replay = self.store.claim_saved_publication_job(120)
        self.assertEqual(replay.id, job.id)
        self.assertIsNotNone(self.store.load_review_snapshot(replay))
        self.assertIsNone(self.store.claim_saved_publication_job(120))
        new_review = self.store.claim_next_pending_job({"claude": 120})
        self.assertEqual(new_review.pr_id, 2)

    def test_restart_releases_round_lease_and_preserves_ambiguous_send(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {})
        self.inline.transition_intent(intent["id"], intent["version"], "sending")
        self.store.recover_abandoned_jobs()
        current = self.inline.claim_round(ready["id"], 120)
        self.assertIsNotNone(current)
        self.assertNotEqual(current["lease_token"], ready["lease_token"])
        self.assertEqual(self.inline.get_intent(intent["id"])["status"], "unknown")
        self.assertEqual(self.inline.get_plan(ready["id"]), {})

    def test_best_effort_rearm_remains_uncertain_when_superseded(self):
        ready = self.ready()
        self.inline.save_plan(ready["id"], ready["lease_token"], {})
        intent = self.inline.reserve_intent(ready["id"], "a", {})
        intent = self.inline.transition_intent(intent["id"], intent["version"], "sending")
        intent = self.inline.transition_intent(intent["id"], intent["version"], "unknown")
        intent = self.inline.transition_intent(intent["id"], intent["version"], "ready")
        self.assertTrue(intent["uncertain"])
        self.assertIsNone(self.inline.transition_intent(intent["id"], intent["version"], "cancelled"))
        self.create(trigger="request", request_comment=("new", "today"))
        intent = self.inline.get_intent(intent["id"])
        self.assertEqual(intent["status"], "unknown")
        self.assertTrue(self.inline.resolve_publication(intent["id"], intent["version"], "absent"))
        intent = self.inline.get_intent(intent["id"])
        self.assertEqual(intent["status"], "cancelled")
        self.assertFalse(intent["uncertain"])

    def test_review_claim_can_exclude_saved_http_publications(self):
        self.store.enqueue_or_update_pr(self.pr, "v1", "v1", "codex")
        job = self.store.claim_next_pending_job({"codex": 120})
        self.store.save_review_snapshot(job, {"source_commit": "source", "publication": {}})
        self.store.mark_retryable_failure(job.id, "HTTP unavailable", 3, job.lease_token, job.running_review_key)
        self.assertIsNone(self.store.claim_next_pending_job({"codex": 120}, exclude_saved_publications=True))
        self.store.enqueue_or_update_pr(replace(self.pr, pr_id=2), "v1", "v1", "codex")
        review = self.store.claim_next_pending_job({"codex": 120}, exclude_saved_publications=True)
        self.assertEqual(review.pr_id, 2)
        replay = self.store.claim_saved_publication_job(120)
        self.assertEqual(replay.id, job.id)
        self.assertIsNotNone(self.store.load_review_snapshot(replay))
