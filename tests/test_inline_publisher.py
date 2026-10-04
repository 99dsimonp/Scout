import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scout.bitbucket import BitbucketError
from scout.config import ConfigError
from scout.deduplication import exact_selection
from scout.inline_publisher import InlinePublisher, comment_eligible, make_candidates
from scout.models import PullRequest
from scout.schema import BITBUCKET_COMMENT_MAX_LENGTH
from scout.state import StateStore
from test_schema import valid_review


def config(providers=("codex", "claude"), identity="bot"):
    return SimpleNamespace(
        agents=SimpleNamespace(providers=list(providers)), bitbucket=SimpleNamespace(bot_account_id=identity),
        queue=SimpleNamespace(publication_settle_seconds=60, publication_max_attempts=3, publication_snapshot_cache_seconds=10),
    )


def comment(comment_id=1, **changes):
    result = {"id": comment_id, "user": {"account_id": "bot"}, "content": {"raw": "body"},
              "deleted": False, "resolved": False, "outdated": False,
              "inline": {"path": "src/app.py", "to": 12}}
    result.update(changes)
    return result


class FakeBitbucket:
    def __init__(self):
        self.comments = []
        self.posts = []
        self.fail = False
        self.account = "bot"

    def current_user(self):
        return {"account_id": self.account}

    def list_pull_request_comments(self, *args, **kwargs):
        return self.comments

    def publish_pull_request_comment(self, repo, pr, content, before_request=None):
        return self._post(content)

    def publish_inline_pull_request_comment(self, repo, pr, path, line, content, **kwargs):
        return self._post(content)

    def _post(self, content):
        self.posts.append(content)
        created = comment(len(self.posts), content={"raw": content}, user={"account_id": self.account})
        self.comments.append(created)
        if self.fail:
            raise BitbucketError("lost response", retryable=True)
        return created


class PublisherUnitTests(unittest.TestCase):
    def test_unknown_comment_state_does_not_suppress(self):
        self.assertTrue(comment_eligible(comment()))
        self.assertTrue(comment_eligible(comment(inline={"path": "src/app.py", "from": 12, "to": None})))
        for key in ("resolved", "outdated", "deleted"):
            unknown = comment()
            del unknown[key]
            self.assertFalse(comment_eligible(unknown))
        for changes in ({"resolved": True}, {"deleted": True}, {"outdated": True}, {"resolution": {"type": "resolution"}}, {"parent": {"id": 1}}):
            self.assertFalse(comment_eligible(comment(**changes)))

    def test_candidate_text_reserves_marker_and_max_severity_before_selection(self):
        result = valid_review()
        result["annotations"][0]["details"] = "long text " * 10000
        record = {"source_commit_hash": "source", "merge_base_hash": "base",
                  "outcomes": [{"provider": "codex", "status": "succeeded", "result": result}]}
        candidates = make_candidates(record)
        self.assertLess(len(candidates[0].rendered_content), BITBUCKET_COMMENT_MAX_LENGTH - 100)
        self.assertNotIn("scout-publication", candidates[0].rendered_content)

    def test_candidates_have_stable_ids_independent_of_provider_finish_order(self):
        outcomes = [{"provider": p, "status": "succeeded", "result": valid_review()} for p in ("codex", "claude")]
        record = {"source_commit_hash": "source", "merge_base_hash": "base", "outcomes": outcomes}
        first = make_candidates(record)
        record["outcomes"].reverse()
        self.assertEqual(first, make_candidates(record))
        self.assertEqual(len(exact_selection(first).retained_ids), 1)


class PublisherIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.db"))
        self.store.initialize()
        self.pr = PullRequest("ws", "repo", 1, "Title", "", "feature", "source", "main", "destination", "base")
        self.bitbucket = FakeBitbucket()
        self.publisher = InlinePublisher(self.store, self.bitbucket, config())
        self.publisher.initialize_identity()

    def ready_round(self, results=None, failed=()):
        if results is None:
            results = {"codex": valid_review(), "claude": valid_review()}
        record = self.store.inline.create_round(self.pr, tuple(results) + tuple(failed), "v1", "v1")
        for provider, result in results.items():
            job = self.store.claim_next_pending_job({provider: 120})
            self.assertTrue(self.store.inline.save_result(job, result))
        for provider in failed:
            self.store.inline.fail_provider(record["id"], provider, "unavailable")
        record = self.store.inline.claim_round(record["id"], 120)
        plan = exact_selection(make_candidates(record)).to_dict()
        self.assertTrue(self.store.inline.save_plan(record["id"], record["lease_token"], plan))
        return record

    def test_duplicate_provider_findings_publish_once_and_resume_without_reselecting(self):
        record = self.ready_round()
        status = self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        self.assertEqual(status, "completed")
        self.assertEqual(len(self.bitbucket.posts), 1)
        outcomes = self.store.inline.candidate_outcomes(record["id"])
        self.assertEqual(sorted(item["status"] for item in outcomes.values()), ["covered", "published"])
        self.assertIn("<!-- scout-publication:", self.bitbucket.posts[0])
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(len(self.bitbucket.posts), 1)

    def test_lost_post_response_is_reconciled_without_second_post(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "publishing")
        intents = self.store.inline.list_intents(round_id=record["id"])
        self.assertEqual(intents[0]["status"], "unknown")
        self.bitbucket.fail = False
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(len(self.bitbucket.posts), 1)
        self.assertEqual(self.store.inline.get_intent(intents[0]["id"])["status"], "published")

    def test_revision_change_posts_pr_comment_with_original_location_and_dependents(self):
        record = self.ready_round()
        moved = replace(self.pr, source_commit_hash="new-source", destination_commit_hash="new-destination", merge_base_hash="new-base")
        with patch.object(self.bitbucket, "publish_inline_pull_request_comment", wraps=self.bitbucket.publish_inline_pull_request_comment) as publish:
            self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: moved), "completed")
        publish.assert_not_called()
        outcomes = self.store.inline.candidate_outcomes(record["id"])
        self.assertEqual(sorted(item["status"] for item in outcomes.values()), ["covered", "published"])
        intent = self.store.inline.list_intents()[0]
        self.assertEqual(intent["kind"], "finding")
        self.assertEqual(self.bitbucket.posts, [intent["payload"]["outdated_content"]])
        self.assertIn("Original location: `src/app.py:12` (NEW)", self.bitbucket.posts[0])
        self.assertIn("original commit `source`", self.bitbucket.posts[0])
        self.assertTrue(self.bitbucket.posts[0].endswith(intent["marker"]))
        self.assertEqual(intent["payload"]["finding"]["source_commit"], "source")
        self.assertEqual(intent["payload"]["finding"]["merge_base"], "base")

    def test_abbreviated_snapshot_hashes_match_full_saved_hashes(self):
        source, destination = "bb34fefb6188" + "1" * 28, "7b3858338be0" + "2" * 28
        self.pr = replace(self.pr, source_commit_hash=source, destination_commit_hash=destination)
        record = self.ready_round()
        current = replace(self.pr, source_commit_hash=source[:12], destination_commit_hash=destination[:12])
        with patch.object(self.bitbucket, "publish_inline_pull_request_comment", wraps=self.bitbucket.publish_inline_pull_request_comment) as publish:
            self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: current), "completed")
        self.assertEqual(publish.call_count, 1)
        self.assertNotIn("Original location:", self.bitbucket.posts[0])

    def test_push_between_posts_moves_remaining_findings_to_pr_comments(self):
        other = valid_review()
        other["annotations"][0].update(summary="Another finding", line=15, line_side="OLD")
        record = self.ready_round({"codex": valid_review(), "claude": other})
        self.publisher.config.queue.publication_snapshot_cache_seconds = 0
        moved = replace(self.pr, source_commit_hash="new-source", merge_base_hash="new-base")

        def snapshot():
            return moved if self.bitbucket.posts else self.pr

        with patch.object(self.bitbucket, "publish_inline_pull_request_comment", wraps=self.bitbucket.publish_inline_pull_request_comment) as publish:
            self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], snapshot), "completed")
        self.assertEqual(publish.call_count, 1)
        intents = sorted(self.store.inline.list_intents(), key=lambda item: item["id"])
        self.assertEqual({item["kind"] for item in intents}, {"finding"})
        # The first finding posts inline before the push; the rest become PR comments.
        self.assertEqual(self.bitbucket.posts, [intents[0]["payload"]["content"], intents[1]["payload"]["outdated_content"]])
        self.assertIn("Original location: `src/app.py:{}` ({})".format(
            intents[1]["payload"]["line"], intents[1]["payload"]["line_side"]), self.bitbucket.posts[1])
        self.assertEqual([item["status"] for item in intents], ["published", "published"])

    def test_partial_results_publish_coverage_notice_first(self):
        record = self.ready_round({"codex": valid_review()}, failed=("claude",))
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(len(self.bitbucket.posts), 2)
        self.assertIn("Claude did not complete", self.bitbucket.posts[0])
        self.assertIn("What I found:", self.bitbucket.posts[1])

    def test_partial_empty_results_post_one_clean_notice_even_after_source_moves(self):
        record = self.ready_round({"codex": {"annotations": []}}, failed=("claude",))
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: replace(self.pr, source_commit_hash="new")), "completed")
        self.assertEqual(len(self.bitbucket.posts), 1)
        self.assertIn("Codex found no material issues", self.bitbucket.posts[0])
        self.assertIn("Claude did not complete", self.bitbucket.posts[0])

    def test_post_author_mismatch_preserves_created_id_and_halts_next_posts(self):
        record = self.ready_round()
        self.bitbucket.account = "unexpected"
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "publication_failed")
        intent = self.store.inline.list_intents(round_id=record["id"])[0]
        self.assertEqual(intent["status"], "published")
        self.assertEqual([str(i) for i in intent["comment_ids"]], ["1"])
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "publication_failed")
        self.assertEqual(len(self.bitbucket.posts), 1)

    def test_identity_configuration_rules(self):
        cases = [(("codex", "claude"), "", None, ConfigError),
                 (("codex",), "", None, None),
                 (("codex", "claude"), "configured", None, None),
                 (("codex",), "configured", "different", ConfigError)]
        for providers, configured, discovered, error in cases:
            with self.subTest(providers=providers, configured=configured, discovered=discovered):
                publisher = InlinePublisher(self.store, self.bitbucket, config(providers, configured))
                with patch.object(self.bitbucket, "current_user", side_effect=BitbucketError("unavailable") if discovered is None else None,
                                  return_value={"account_id": discovered}):
                    if error:
                        with self.assertRaises(error):
                            publisher.initialize_identity()
                    else:
                        publisher.initialize_identity()
                        self.assertEqual(publisher.identity, configured or discovered)

    def test_negative_reconciliation_waits_then_resends_same_marker(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        original = self.bitbucket.posts[0]
        self.bitbucket.comments.clear()
        self.bitbucket.fail = False
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "publishing")
        self.assertEqual(len(self.bitbucket.posts), 1)
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set last_attempt_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(self.bitbucket.posts, [original, original])

    def test_negative_lookup_resends_original_payload_after_revision_change(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        self.bitbucket.comments.clear()
        self.bitbucket.fail = False
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set last_attempt_at='2000-01-01T00:00:00+00:00'")
        moved = replace(self.pr, source_commit_hash="changed", merge_base_hash="new-base")
        with patch.object(self.bitbucket, "publish_inline_pull_request_comment", wraps=self.bitbucket.publish_inline_pull_request_comment) as publish:
            self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: moved), "completed")
        publish.assert_not_called()
        intent = self.store.inline.list_intents()[0]
        self.assertEqual(intent["status"], "published")
        # The resend keeps the marker but no longer anchors the old line in the new diff.
        self.assertEqual(self.bitbucket.posts, [intent["payload"]["content"], intent["payload"]["outdated_content"]])
        self.assertTrue(all(post.endswith(intent["marker"]) for post in self.bitbucket.posts))

    def test_negative_lookup_must_start_after_settling_before_resend(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        intent = self.store.inline.list_intents()[0]
        self.bitbucket.comments.clear()
        self.bitbucket.fail = False
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set last_attempt_at='1970-01-01T00:00:00+00:00'")
        # A restarted worker starts pagination before the settling deadline;
        # crossing the deadline during that lookup cannot prove remote absence.
        with patch("scout.inline_publisher.time.time", return_value=59) as now:
            def lookup(*args, **kwargs):
                now.return_value = 61
                return []
            with patch.object(self.bitbucket, "list_pull_request_comments", side_effect=lookup):
                self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "publishing")
            self.assertEqual(len(self.bitbucket.posts), 1)
            self.assertEqual(self.store.inline.get_intent(intent["id"])["status"], "unknown")
            self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(self.bitbucket.posts, [intent["payload"]["content"]] * 2)

    def test_push_after_negative_lookup_does_not_cancel_resend(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        self.bitbucket.comments.clear()
        self.bitbucket.fail = False
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set last_attempt_at='2000-01-01T00:00:00+00:00'")
        snapshots = iter([self.pr, replace(self.pr, source_commit_hash="changed")])
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: next(snapshots)), "completed")
        intent = self.store.inline.list_intents()[0]
        self.assertEqual(intent["status"], "published")
        self.assertFalse(intent["uncertain"])
        self.assertEqual(self.bitbucket.posts, [intent["payload"]["content"], intent["payload"]["outdated_content"]])

    def test_closed_pr_blocks_even_unanchored_coverage_notice(self):
        record = self.ready_round({"codex": valid_review()}, failed=("claude",))
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: replace(self.pr, state="MERGED")), "cancelled")
        self.assertEqual(self.bitbucket.posts, [])

    def test_author_halt_during_preflight_prevents_next_post(self):
        record = self.ready_round()
        def snapshot():
            self.publisher.halted = True
            return self.pr
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], snapshot), "publication_failed")
        self.assertEqual(self.bitbucket.posts, [])

    def test_late_duplicate_ids_are_recorded_without_another_post(self):
        record = self.ready_round()
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        duplicate = dict(self.bitbucket.comments[0], id=23)
        self.bitbucket.comments.append(duplicate)
        self.publisher.history(record)
        intent = self.store.inline.list_intents()[0]
        self.assertEqual(set(intent["comment_ids"]), {"1", "23"})
        self.assertEqual(intent["status"], "published")
        self.assertEqual(len(self.bitbucket.posts), 1)

    def test_failed_listing_never_counts_as_negative_reconciliation(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set last_attempt_at='2000-01-01T00:00:00+00:00'")
        with patch.object(self.bitbucket, "list_pull_request_comments", side_effect=BitbucketError("unavailable")):
            self.assertFalse(self.publisher.reconcile(record))
        self.assertEqual(len(self.bitbucket.posts), 1)
        self.assertEqual(self.store.inline.list_intents()[0]["status"], "unknown")

    def test_resolved_history_is_excluded_without_hiding_older_eligible_roots(self):
        record = self.ready_round()
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        self.assertEqual(len(self.publisher.history(record)), 1)
        self.bitbucket.comments[0]["resolved"] = True
        self.assertEqual(self.publisher.history(record), [])

    def test_historical_target_disappearing_adds_original_comment_to_saved_plan(self):
        record = self.ready_round()
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        history = self.publisher.history(record)
        self.store.inline.release_round(record["id"], record["lease_token"], status="completed")
        later = self.store.inline.create_round(self.pr, ("codex",), "v1", "v1", trigger="request")
        job = self.store.claim_next_pending_job({"codex": 120})
        self.store.inline.save_result(job, valid_review())
        later = self.store.inline.claim_round(later["id"], 120)
        plan = exact_selection(make_candidates(later), history).to_dict()
        self.assertEqual(plan["retained_ids"], [])
        self.store.inline.save_plan(later["id"], later["lease_token"], plan)
        self.bitbucket.comments[0]["deleted"] = True
        self.assertEqual(self.publisher.publish_round(later["id"], later["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(len(self.bitbucket.posts), 2)
        self.assertEqual(self.store.inline.get_plan(later["id"]), plan)
        self.assertNotIn("no material issues", self.bitbucket.posts[-1])

    def test_old_side_history_with_null_to_suppresses_identical_rerun(self):
        result = valid_review()
        result["annotations"][0]["line_side"] = "OLD"
        record = self.ready_round({"codex": result})
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")
        self.bitbucket.comments[0]["inline"] = {"path": "src/app.py", "from": 12, "to": None}
        self.store.inline.release_round(record["id"], record["lease_token"], status="completed")
        later = self.store.inline.create_round(self.pr, ("codex",), "v1", "v1", trigger="request")
        job = self.store.claim_next_pending_job({"codex": 120})
        self.store.inline.save_result(job, result)
        later = self.store.inline.claim_round(later["id"], 120)
        plan = exact_selection(make_candidates(later), self.publisher.history(later)).to_dict()
        self.assertEqual(plan["retained_ids"], [])
        self.store.inline.save_plan(later["id"], later["lease_token"], plan)
        self.assertEqual(self.publisher.publish_round(later["id"], later["lease_token"], lambda: self.pr), "completed")
        self.assertEqual(len(self.bitbucket.posts), 1)
        self.assertEqual(self.store.inline.candidate_outcomes(later["id"])["codex:finding-001"]["status"], "covered")

    def test_legacy_old_side_history_uses_non_null_anchor(self):
        record = self.ready_round()
        self.bitbucket.comments = [comment(
            inline={"path": "src/app.py", "from": 12, "to": None},
            content={"raw": "Deleted error check\n\nScout: High issue found by Codex."},
        )]
        history = self.publisher.history(record)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].annotation["line"], 12)
        self.assertEqual(history[0].annotation["line_side"], "OLD")
        self.assertEqual(self.publisher.history(record), history)

    def test_operator_confirmed_absence_resends_original_finding_after_push(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        intent = self.store.inline.list_intents()[0]
        self.assertTrue(self.store.inline.resolve_publication(intent["id"], intent["version"], "absent"))
        self.bitbucket.fail = False
        self.bitbucket.comments.clear()
        moved = replace(self.pr, source_commit_hash="moved")
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: moved), "completed")
        self.assertEqual(self.bitbucket.posts, [intent["payload"]["content"], intent["payload"]["outdated_content"]])
        self.assertEqual(self.store.inline.get_intent(intent["id"])["status"], "published")

    def test_author_aliases_from_discovery_are_accepted(self):
        with patch.object(self.bitbucket, "current_user", return_value={"account_id": "bot", "uuid": "{bot-uuid}"}):
            self.publisher.initialize_identity()
        record = self.ready_round()
        self.bitbucket.account = "{bot-uuid}"
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr), "completed")

    def test_author_mismatch_remains_halted_after_restart(self):
        record = self.ready_round()
        self.bitbucket.account = "unexpected"
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        self.bitbucket.account = "bot"
        with self.assertRaisesRegex(ConfigError, "POST author mismatch"):
            InlinePublisher(self.store, self.bitbucket, config()).initialize_identity()

    def test_closed_pr_only_reconciles_and_never_resends(self):
        record = self.ready_round()
        self.bitbucket.fail = True
        self.publisher.publish_round(record["id"], record["lease_token"], lambda: self.pr)
        self.bitbucket.comments.clear()
        with self.store.connect() as conn:
            conn.execute("update inline_publication_intents set last_attempt_at='2000-01-01T00:00:00+00:00'")
        self.publisher.close_pr("ws", "repo", 1)
        self.assertEqual(len(self.bitbucket.posts), 1)
        self.assertEqual(self.store.inline.list_intents(), [])
