import base64
import json
import unittest
from io import BytesIO
from unittest.mock import patch
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


if __name__ == "__main__":
    unittest.main()
