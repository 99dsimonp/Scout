from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .models import PullRequest

LOG = logging.getLogger(__name__)


class BitbucketError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BitbucketNotFound(BitbucketError):
    pass


@dataclass(frozen=True)
class BitbucketCredentials:
    username: str
    api_key: str


class BitbucketClient:
    def __init__(self, base_url: str, workspace: str, credentials: BitbucketCredentials, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.workspace = workspace
        self.credentials = credentials
        self.timeout = timeout

    def list_open_pull_requests(self, repo_slug: str, pagelen: int = 50) -> List[PullRequest]:
        fields = ",".join(
            [
                "values.id",
                "values.title",
                "values.description",
                "values.updated_on",
                "values.source.branch.name",
                "values.source.commit.hash",
                "values.draft",
                "values.destination.branch.name",
                "values.destination.commit.hash",
                "next",
            ]
        )
        query = urlencode({"state": "OPEN", "pagelen": str(pagelen), "fields": fields})
        url = "{}/repositories/{}/{}/pullrequests?{}".format(self.base_url, self.workspace, repo_slug, query)
        prs: List[PullRequest] = []
        while url:
            payload = self._request_json("GET", url)
            for item in payload.get("values", []):
                prs.append(self._parse_pr(repo_slug, item))
            url = payload.get("next")
        return prs

    def validate_repository(self, repo_slug: str) -> None:
        query = urlencode({"fields": "uuid"})
        url = "{}/repositories/{}/{}?{}".format(self.base_url, self.workspace, repo_slug, query)
        self._request_json("GET", url)

    def publish_report(self, repo_slug: str, commit_hash: str, report_id: str, report: Dict[str, Any]) -> None:
        path = "/repositories/{}/{}/commit/{}/reports/{}".format(
            self.workspace, repo_slug, commit_hash, report_id
        )
        self._request_json("PUT", self.base_url + path, report)

    def report_exists(self, repo_slug: str, commit_hash: str, report_id: str) -> bool:
        path = "/repositories/{}/{}/commit/{}/reports/{}".format(
            self.workspace, repo_slug, commit_hash, report_id
        )
        try:
            self._request_json("GET", self.base_url + path)
        except BitbucketNotFound:
            return False
        return True

    def publish_annotations(
        self,
        repo_slug: str,
        commit_hash: str,
        report_id: str,
        annotations: Iterable[Dict[str, Any]],
        before_request: Optional[Callable[[], None]] = None,
    ) -> None:
        desired = list(annotations)
        existing = self.list_annotations(repo_slug, commit_hash, report_id, before_request=before_request)
        desired_ids: Set[str] = {annotation["external_id"] for annotation in desired}
        existing_ids: Set[str] = {
            annotation["external_id"]
            for annotation in existing
            if annotation.get("external_id")
        }

        for annotation in desired:
            external_id = _quote_external_id(annotation["external_id"])
            path = "/repositories/{}/{}/commit/{}/reports/{}/annotations/{}".format(
                self.workspace, repo_slug, commit_hash, report_id, external_id
            )
            if before_request is not None:
                before_request()
            self._request_json("PUT", self.base_url + path, annotation)

        for external_id in sorted(existing_ids - desired_ids):
            path = "/repositories/{}/{}/commit/{}/reports/{}/annotations/{}".format(
                self.workspace,
                repo_slug,
                commit_hash,
                report_id,
                _quote_external_id(external_id),
            )
            if before_request is not None:
                before_request()
            self._request_json("DELETE", self.base_url + path)

    def list_annotations(
        self,
        repo_slug: str,
        commit_hash: str,
        report_id: str,
        before_request: Optional[Callable[[], None]] = None,
    ) -> List[Dict[str, Any]]:
        path = "/repositories/{}/{}/commit/{}/reports/{}/annotations?{}".format(
            self.workspace,
            repo_slug,
            commit_hash,
            report_id,
            urlencode({"pagelen": "100"}),
        )
        url = self.base_url + path
        annotations: List[Dict[str, Any]] = []
        while url:
            if before_request is not None:
                before_request()
            payload = self._request_json("GET", url)
            annotations.extend(payload.get("values", []))
            url = payload.get("next")
        return annotations

    def publish_pull_request_comment(
        self,
        repo_slug: str,
        pr_id: int,
        content: str,
        before_request: Optional[Callable[[], None]] = None,
    ) -> None:
        body = {"content": {"raw": content}}
        path = "/repositories/{}/{}/pullrequests/{}/comments".format(
            self.workspace, repo_slug, pr_id
        )
        if before_request is not None:
            before_request()
        self._request_json("POST", self.base_url + path, body)

    def list_pull_request_comments(
        self,
        repo_slug: str,
        pr_id: int,
        before_request: Optional[Callable[[], None]] = None,
    ) -> List[Dict[str, Any]]:
        fields = ",".join(
            [
                "values.id",
                "values.content.raw",
                "values.updated_on",
                "values.deleted",
                "values.inline",
                "values.user.account_id",
                "values.user.nickname",
                "values.user.username",
                "values.user.uuid",
                "next",
            ]
        )
        path = "/repositories/{}/{}/pullrequests/{}/comments?{}".format(
            self.workspace,
            repo_slug,
            pr_id,
            urlencode({"pagelen": "100", "fields": fields}),
        )
        url = self.base_url + path
        comments: List[Dict[str, Any]] = []
        while url:
            if before_request is not None:
                before_request()
            payload = self._request_json("GET", url)
            comments.extend(payload.get("values", []))
            url = payload.get("next")
        return comments

    def publish_inline_pull_request_comment(
        self,
        repo_slug: str,
        pr_id: int,
        path: str,
        line: int,
        content: str,
        before_request: Optional[Callable[[], None]] = None,
    ) -> None:
        body = {
            "content": {"raw": content},
            "inline": {"path": path, "to": line},
        }
        request_path = "/repositories/{}/{}/pullrequests/{}/comments".format(
            self.workspace, repo_slug, pr_id
        )
        if before_request is not None:
            before_request()
        self._request_json("POST", self.base_url + request_path, body)

    def _parse_pr(self, repo_slug: str, item: Dict[str, Any]) -> PullRequest:
        source = item.get("source") or {}
        destination = item.get("destination") or {}
        source_branch = (source.get("branch") or {}).get("name") or ""
        destination_branch = (destination.get("branch") or {}).get("name") or ""
        source_commit = (source.get("commit") or {}).get("hash") or ""
        destination_commit = (destination.get("commit") or {}).get("hash")
        if not source_commit:
            raise BitbucketError("PR {} missing source commit".format(item.get("id")))
        return PullRequest(
            workspace=self.workspace,
            repo_slug=repo_slug,
            pr_id=int(item["id"]),
            title=item.get("title") or "",
            description=item.get("description") or "",
            source_branch=source_branch,
            source_commit_hash=source_commit,
            destination_branch=destination_branch,
            destination_commit_hash=destination_commit,
            is_draft=(item.get("draft") is True),
        )

    def _request_json(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic {}".format(self._basic_auth_token()),
            "User-Agent": "scout",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            exc.close()
            if exc.code == 404:
                raise BitbucketNotFound("Bitbucket HTTP 404 for {}".format(url), retryable=False) from exc
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            raise BitbucketError("Bitbucket HTTP {} for {}".format(exc.code, url), retryable=retryable) from exc
        except URLError as exc:
            raise BitbucketError("Bitbucket request failed for {}: {}".format(url, exc), retryable=True) from exc
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BitbucketError("Bitbucket returned invalid JSON for {}".format(url), retryable=True) from exc
        if not isinstance(parsed, dict):
            raise BitbucketError("Bitbucket returned unexpected JSON for {}".format(url), retryable=True)
        return parsed

    def _basic_auth_token(self) -> str:
        raw = "{}:{}".format(self.credentials.username, self.credentials.api_key).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")


def _quote_external_id(external_id: str) -> str:
    return quote(external_id, safe="")
