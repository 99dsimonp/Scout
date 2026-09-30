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

    def test_stale_findings_preserve_dependents_and_post_snapshot_notice(self):
        record = self.ready_round()
        moved = replace(self.pr, source_commit_hash="new-source")
        self.assertEqual(self.publisher.publish_round(record["id"], record["lease_token"], lambda: moved), "completed_with_stale_findings")
        self.assertEqual(len(self.bitbucket.posts), 1)
        self.assertIn("found 2 issues", self.bitbucket.posts[0])
        self.assertIn("`source`", self.bitbucket.posts[0])
        self.assertTrue(all(item["status"] == "stale_unpublished" for item in self.store.inline.candidate_outcomes(record["id"]).values()))

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
