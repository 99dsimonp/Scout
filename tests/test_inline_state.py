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

    def test_atomic_explicit_request_and_replacement(self):
        round_ = self.create(request_comment=("1", "today"), trigger="request")
        self.assertTrue(self.store.processed_pull_request_comment_review_requested("ws", "repo", 1, "1", "today"))
        self.assertIsNone(self.create(request_comment=("1", "today"), trigger="request"))
        old_job = self.store.claim_next_pending_job({"codex": 120})
        changed = replace(self.pr, source_commit_hash="new")
        newer = self.inline.create_round(changed, ("codex", "claude"), "v1", "v1", trigger="replacement", replace_round_id=round_["id"], expected_version=round_["version"])
        self.assertIsNotNone(newer)
        self.assertFalse(self.inline.save_result(old_job, {"findings": []}))
        self.assertEqual(self.inline.get_round(round_["id"])["status"], "superseded")

    def test_ready_boundary_prevents_source_replacement(self):
        ready = self.ready()
        self.assertIsNone(self.inline.create_round(replace(self.pr, source_commit_hash="new"), ("codex",), "v1", "v1", trigger="replacement", replace_round_id=ready["id"], expected_version=ready["version"]))

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
