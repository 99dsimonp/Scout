import base64
import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.error import HTTPError

from scout.bitbucket import BitbucketClient, BitbucketCredentials, BitbucketError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class BitbucketTests(unittest.TestCase):
    def test_malformed_open_pr_inventory_is_retryable_not_empty(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("bot", "secret"))
        malformed = ({}, {"values": None}, {"values": {}}, {"values": [None]},
                     {"values": [{"id": 1}]}, {"values": [{"id": True, "source": {"commit": {"hash": "abc"}}}]},
                     {"values": [{"id": 1, "source": {"commit": {"hash": ["abc"]}}}]},
                     {"values": [{"id": 1, "source": "malformed"}]})
        for payload in malformed:
            with self.subTest(payload=payload), patch.object(client, "_request_json", return_value=payload):
                with self.assertRaises(BitbucketError) as raised:
                    client.list_open_pull_requests("repo")
                self.assertTrue(raised.exception.retryable)

    def test_open_pr_inventory_requires_valid_nonrepeating_next(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("bot", "secret"))
        for next_page in (False, 123, "", "/relative", "https://other.example/prs"):
            with self.subTest(next_page=next_page), patch.object(client, "_request_json", side_effect=[{"values": [], "next": next_page}] * 3):
                with self.assertRaises(BitbucketError) as raised:
                    client.list_open_pull_requests("repo")
                self.assertTrue(raised.exception.retryable)
        responses = [{"values": [], "next": "https://api.bitbucket.org/2.0/repeat"}] * 3
        with patch.object(client, "_request_json", side_effect=responses):
            with self.assertRaisesRegex(BitbucketError, "repeated a page") as raised:
                client.list_open_pull_requests("repo")
            self.assertTrue(raised.exception.retryable)

    def test_malformed_later_inventory_page_does_not_return_partial_prs(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("bot", "secret"))
        pr = {"id": 1, "source": {"commit": {"hash": "abc"}}}
        pages = [{"values": [pr], "next": "https://api.bitbucket.org/2.0/page2"}, {}]
        with patch.object(client, "_request_json", side_effect=pages):
            with self.assertRaises(BitbucketError) as raised:
                client.list_open_pull_requests("repo")
            self.assertTrue(raised.exception.retryable)

    def test_incomplete_comments_response_cannot_prove_publication_absent(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("bot", "secret"))
        for payload in ({}, {"values": [{}]}, {"values": [], "next": 123}):
            with self.subTest(payload=payload), patch.object(client, "_request_json", return_value=payload):
                with self.assertRaises(BitbucketError):
                    client.list_pull_request_comments("repo", 1)

    def test_repeated_comment_page_is_an_incomplete_lookup(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("bot", "secret"))
        with patch.object(client, "_request_json", return_value={"values": [], "next": "https://api.bitbucket.org/2.0/repeat"}):
            with self.assertRaisesRegex(BitbucketError, "repeated a page"):
                client.list_pull_request_comments("repo", 1)

    def test_comment_writes_return_created_id_and_author(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("alice", "secret"))
        created = {"id": 12, "user": {"account_id": "bot"}}
        with patch("scout.bitbucket.urlopen", return_value=FakeResponse(created)):
            self.assertEqual(client.publish_pull_request_comment("repo", 9, "body"), created)
            self.assertEqual(client.publish_inline_pull_request_comment("repo", 9, "app.py", 2, "body"), created)

    def test_current_user_exposes_immutable_identity(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("alice", "secret"))
        with patch("scout.bitbucket.urlopen", return_value=FakeResponse({"account_id": "bot"})) as request:
            self.assertEqual(client.current_user(), {"account_id": "bot"})
        self.assertTrue(request.call_args.args[0].full_url.endswith("/user"))

    def test_get_pull_request_reads_both_revisions_after_lease_callback(self):
        client = BitbucketClient("https://api.bitbucket.org/2.0", "ws", BitbucketCredentials("bot", "secret"))
        callbacks = []

        def request(method, url):
            self.assertEqual(callbacks, ["checked"])
            self.assertEqual(method, "GET")
            self.assertIn("/repositories/ws/repo/pullrequests/13?", url)
            self.assertIn("source.commit.hash", url)
            self.assertIn("destination.commit.hash", url)
            return {"id": 13, "source": {"commit": {"hash": "a" * 40}}, "destination": {"commit": {"hash": "b" * 40}}}

        with patch.object(client, "_request_json", side_effect=request):
            pr = client.get_pull_request("repo", 13, before_request=lambda: callbacks.append("checked"))
        self.assertEqual(pr.source_commit_hash, "a" * 40)
        self.assertEqual(pr.destination_commit_hash, "b" * 40)

    def test_basic_auth_header_is_sent(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["auth"] = request.headers["Authorization"]
            seen["user_agent"] = request.headers["User-agent"]
            seen["url"] = request.full_url
            return FakeResponse({"values": [], "next": None})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            self.assertEqual(client.list_open_pull_requests("repo"), [])
        expected = base64.b64encode(b"alice:secret").decode("ascii")
        self.assertEqual(seen["auth"], "Basic " + expected)
        self.assertEqual(seen["user_agent"], "scout")
        self.assertIn("values.draft", seen["url"])

    def test_oauth_client_credentials_token_is_exchanged_and_cached(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(
                {
                    "method": request.get_method(),
                    "url": request.full_url,
                    "auth": request.headers["Authorization"],
                    "content_type": request.headers.get("Content-type"),
                    "data": request.data,
                }
            )
            if request.full_url == "https://bitbucket.org/site/oauth2/access_token":
                return FakeResponse({"access_token": "access-token", "expires_in": 3600})
            return FakeResponse({"values": [], "next": None})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials(
                "",
                "",
                auth_type="oauth_client_credentials",
                oauth_client_id="client-id",
                oauth_client_secret="client-secret",
            ),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            self.assertEqual(client.list_open_pull_requests("repo"), [])
            self.assertEqual(client.list_open_pull_requests("repo"), [])

        expected_client_auth = base64.b64encode(b"client-id:client-secret").decode("ascii")
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["url"], "https://bitbucket.org/site/oauth2/access_token")
        self.assertEqual(requests[0]["auth"], "Basic " + expected_client_auth)
        self.assertEqual(requests[0]["content_type"], "application/x-www-form-urlencoded")
        self.assertEqual(parse_qs(requests[0]["data"].decode("utf-8")), {"grant_type": ["client_credentials"]})
        self.assertEqual([request["auth"] for request in requests[1:]], ["Bearer access-token", "Bearer access-token"])

    def test_list_open_pull_requests_parses_draft_status(self):
        def fake_urlopen(request, timeout):
            return FakeResponse(
                {
                    "values": [
                        {
                            "id": 13,
                            "title": "Draft",
                            "source": {"branch": {"name": "feature"}, "commit": {"hash": "a" * 40}},
                            "destination": {
                                "branch": {"name": "main"},
                                "commit": {"hash": "b" * 40},
                            },
                            "draft": True,
                        }
                    ],
                    "next": None,
                }
            )

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            prs = client.list_open_pull_requests("repo")

        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0].pr_id, 13)
        self.assertTrue(prs[0].is_draft)
        self.assertEqual(prs[0].source_commit_hash, "a" * 40)

    def test_report_exists_checks_commit_report(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request.get_method(), request.full_url))
            return FakeResponse({"uuid": "report"})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            self.assertTrue(client.report_exists("repo", "abc123", "report-id"))

        expected_url = "https://api.bitbucket.org/2.0/repositories/ws/repo/commit/abc123/reports/report-id"
        self.assertEqual(
            requests,
            [("GET", expected_url)],
        )

    def test_validate_repository_checks_repo_endpoint(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request.get_method(), request.full_url))
            return FakeResponse({"uuid": "repo"})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.validate_repository("repo")

        self.assertEqual(
            requests,
            [("GET", "https://api.bitbucket.org/2.0/repositories/ws/repo?fields=uuid")],
        )

    def test_report_exists_returns_false_for_missing_report(self):
        def fake_urlopen(request, timeout):
            raise HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=BytesIO(b""))

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            self.assertFalse(client.report_exists("repo", "abc123", "report-id"))

    def test_publish_annotations_removes_stale_annotations(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request.get_method(), request.full_url, request.data))
            if request.get_method() == "GET":
                return FakeResponse(
                    {
                        "values": [
                            {"external_id": "keep"},
                            {"external_id": "stale/id"},
                        ],
                        "next": None,
                    }
                )
            return FakeResponse({})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        annotations = [
            {
                "external_id": "keep",
                "annotation_type": "BUG",
                "path": "src/app.py",
                "line": 1,
                "summary": "Keep",
                "details": "Updated",
                "severity": "MEDIUM",
                "result": "FAILED",
            },
            {
                "external_id": "new",
                "annotation_type": "BUG",
                "path": "src/app.py",
                "line": 2,
                "summary": "New",
                "details": "Created",
                "severity": "MEDIUM",
                "result": "FAILED",
            },
        ]
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_annotations("repo", "abc123", "report-id", annotations)

        self.assertEqual([request[0] for request in requests], ["GET", "PUT", "PUT", "DELETE"])
        self.assertIn("/annotations?pagelen=100", requests[0][1])
        self.assertTrue(requests[1][1].endswith("/annotations/keep"))
        self.assertTrue(requests[2][1].endswith("/annotations/new"))
        self.assertTrue(requests[3][1].endswith("/annotations/stale%2Fid"))

    def test_publish_annotations_calls_before_request_for_each_request(self):
        requests = []
        heartbeats = []

        def fake_urlopen(request, timeout):
            requests.append(request.get_method())
            if request.get_method() == "GET":
                return FakeResponse({"values": [{"external_id": "stale"}], "next": None})
            return FakeResponse({})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        annotations = [
            {
                "external_id": "new",
                "annotation_type": "BUG",
                "path": "src/app.py",
                "line": 1,
                "summary": "New",
                "details": "Created",
                "severity": "MEDIUM",
                "result": "FAILED",
            }
        ]
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_annotations(
                "repo",
                "abc123",
                "report-id",
                annotations,
                before_request=lambda: heartbeats.append("renew"),
            )

        self.assertEqual(requests, ["GET", "PUT", "DELETE"])
        self.assertEqual(heartbeats, ["renew", "renew", "renew"])

    def test_publish_pull_request_comment_creates_comment(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request.get_method(), request.full_url, request.data))
            return FakeResponse({"id": 12})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_pull_request_comment("repo", 9, "body")

        self.assertEqual([request[0] for request in requests], ["POST"])
        self.assertTrue(requests[0][1].endswith("/pullrequests/9/comments"))
        self.assertEqual(json.loads(requests[0][2].decode("utf-8")), {"content": {"raw": "body"}})

    def test_publish_pull_request_comment_posts_each_time(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request.get_method(), request.full_url, request.data))
            return FakeResponse({"id": len(requests)})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_pull_request_comment("repo", 9, "first")
            client.publish_pull_request_comment("repo", 9, "second")

        self.assertEqual([request[0] for request in requests], ["POST", "POST"])
        self.assertEqual(json.loads(requests[0][2].decode("utf-8")), {"content": {"raw": "first"}})
        self.assertEqual(json.loads(requests[1][2].decode("utf-8")), {"content": {"raw": "second"}})

    def test_publish_pull_request_comment_calls_before_request(self):
        requests = []
        heartbeats = []

        def fake_urlopen(request, timeout):
            requests.append(request.get_method())
            return FakeResponse({"id": 12})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_pull_request_comment(
                "repo",
                9,
                "body",
                before_request=lambda: heartbeats.append("renew"),
            )

        self.assertEqual(requests, ["POST"])
        self.assertEqual(heartbeats, ["renew"])

    def test_list_pull_request_comments_paginates_with_required_fields(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request.full_url)
            if len(requests) == 1:
                return FakeResponse(
                    {
                        "values": [
                            {
                                "id": 1,
                                "content": {"raw": "@Scout review this"},
                                "created_on": "2026-06-22T09:00:00+00:00",
                                "user": {"account_id": "author-1", "display_name": "Alice"},
                                "updated_on": "2026-06-22T10:00:00+00:00",
                                "deleted": False,
                                "inline": {"path": "src/app.py", "to": 12},
                            }
                        ],
                        "next": "https://api.bitbucket.org/2.0/next-page",
                    }
                )
            return FakeResponse(
                {
                    "values": [
                        {
                            "id": 2,
                            "parent": {"id": 1},
                            "content": {"raw": "later"},
                            "updated_on": "2026-06-22T10:01:00+00:00",
                            "deleted": True,
                        }
                    ],
                    "next": None,
                }
            )

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            comments = client.list_pull_request_comments("repo", 9)

        self.assertEqual([comment["id"] for comment in comments], [1, 2])
        self.assertEqual(comments[1]["parent"], {"id": 1})
        self.assertEqual(comments[0]["user"]["display_name"], "Alice")
        self.assertEqual(requests[1], "https://api.bitbucket.org/2.0/next-page")
        self.assertIn("values.content.raw", requests[0])
        self.assertIn("values.parent.id", requests[0])
        self.assertIn("values.created_on", requests[0])
        self.assertIn("values.user.display_name", requests[0])
        self.assertIn("values.updated_on", requests[0])
        self.assertIn("values.deleted", requests[0])
        self.assertIn("values.inline", requests[0])
        self.assertIn("values.user.nickname", requests[0])
        self.assertIn("values.user.account_id", requests[0])

    def test_list_pull_request_comments_calls_before_request_for_each_page(self):
        heartbeats = []

        def fake_urlopen(request, timeout):
            if len(heartbeats) == 1:
                return FakeResponse({"values": [], "next": "https://api.bitbucket.org/2.0/next-page"})
            return FakeResponse({"values": [], "next": None})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.list_pull_request_comments(
                "repo",
                9,
                before_request=lambda: heartbeats.append("renew"),
            )

        self.assertEqual(heartbeats, ["renew", "renew"])

    def test_publish_inline_pull_request_comment_creates_inline_comment(self):
        requests = []
        heartbeats = []

        def fake_urlopen(request, timeout):
            requests.append((request.get_method(), request.full_url, request.data))
            return FakeResponse({"id": 12})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_inline_pull_request_comment(
                "repo",
                9,
                "src/app.py",
                12,
                "body",
                before_request=lambda: heartbeats.append("renew"),
            )

        self.assertEqual([request[0] for request in requests], ["POST"])
        self.assertTrue(requests[0][1].endswith("/pullrequests/9/comments"))
        self.assertEqual(
            json.loads(requests[0][2].decode("utf-8")),
            {"content": {"raw": "body"}, "inline": {"path": "src/app.py", "to": 12}},
        )
        self.assertEqual(heartbeats, ["renew"])

    def test_publish_inline_pull_request_comment_targets_old_side(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request.data)
            return FakeResponse({"id": 12})

        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )
        with patch("scout.bitbucket.urlopen", fake_urlopen):
            client.publish_inline_pull_request_comment(
                "repo",
                9,
                "src/app.py",
                11,
                "body",
                line_side="OLD",
            )

        self.assertEqual(
            json.loads(requests[0].decode("utf-8")),
            {"content": {"raw": "body"}, "inline": {"path": "src/app.py", "from": 11}},
        )

    def test_publish_inline_pull_request_comment_rejects_unknown_side(self):
        client = BitbucketClient(
            "https://api.bitbucket.org/2.0",
            "ws",
            BitbucketCredentials("alice", "secret"),
        )

        with self.assertRaisesRegex(BitbucketError, "line_side must be NEW or OLD"):
            client.publish_inline_pull_request_comment(
                "repo",
                9,
                "src/app.py",
                12,
                "body",
                line_side="BOTH",
            )


if __name__ == "__main__":
    unittest.main()
