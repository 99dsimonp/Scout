import base64
import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.error import HTTPError

from scout.bitbucket import BitbucketClient, BitbucketCredentials


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
        self.assertEqual(requests[1], "https://api.bitbucket.org/2.0/next-page")
        self.assertIn("values.content.raw", requests[0])
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


if __name__ == "__main__":
    unittest.main()
