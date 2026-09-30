from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import quote, urlencode, urlsplit
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
    auth_type: str = "basic"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = "https://bitbucket.org/site/oauth2/access_token"


@dataclass
class _OAuthToken:
    access_token: str
    expires_at: float


class BitbucketClient:
    def __init__(self, base_url: str, workspace: str, credentials: BitbucketCredentials, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.workspace = workspace
        self.credentials = credentials
        self.timeout = timeout
        self._oauth_token: Optional[_OAuthToken] = None

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
        visited: Set[str] = set()
        origin = urlsplit(self.base_url)
        while url:
            if url in visited:
                raise BitbucketError("Bitbucket PR pagination repeated a page", retryable=True)
            visited.add(url)
            payload = self._request_json("GET", url)
            values = payload.get("values")
            if not isinstance(values, list):
                raise BitbucketError("Bitbucket PR inventory is incomplete", retryable=True)
            for item in values:
                try:
                    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
                        raise ValueError("invalid PR ID")
                    pr = self._parse_pr(repo_slug, item)
                    if any(not isinstance(value, str) for value in (
                        pr.source_commit_hash, pr.source_branch, pr.destination_branch, pr.title, pr.description
                    )) or (pr.destination_commit_hash is not None and not isinstance(pr.destination_commit_hash, str)):
                        raise ValueError("invalid PR metadata")
                except (BitbucketError, AttributeError, KeyError, TypeError, ValueError) as exc:
                    raise BitbucketError("Bitbucket PR inventory contains an invalid entry", retryable=True) from exc
                prs.append(pr)
            next_page = payload.get("next")
            if next_page is not None:
                if not isinstance(next_page, str) or not next_page:
                    raise BitbucketError("Bitbucket PR pagination is invalid", retryable=True)
                try:
                    parsed_next = urlsplit(next_page)
                except ValueError as exc:
                    raise BitbucketError("Bitbucket PR pagination is invalid", retryable=True) from exc
                if (parsed_next.scheme, parsed_next.netloc) != (origin.scheme, origin.netloc):
                    raise BitbucketError("Bitbucket PR pagination changed API origin", retryable=True)
            url = payload.get("next")
        return prs

    def validate_repository(self, repo_slug: str) -> None:
        query = urlencode({"fields": "uuid"})
        url = "{}/repositories/{}/{}?{}".format(self.base_url, self.workspace, repo_slug, query)
        self._request_json("GET", url)

    def current_user(self) -> Dict[str, Any]:
        return self._request_json("GET", self.base_url + "/user")

    def get_pull_request(
        self,
        repo_slug: str,
        pr_id: int,
        before_request: Optional[Callable[[], None]] = None,
    ) -> PullRequest:
        query = urlencode({"fields": "id,title,description,draft,state,source.commit.hash,source.branch.name,destination.commit.hash,destination.branch.name"})
        path = "/repositories/{}/{}/pullrequests/{}?{}".format(self.workspace, repo_slug, pr_id, query)
        if before_request is not None:
            before_request()
        return self._parse_pr(repo_slug, self._request_json("GET", self.base_url + path))

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
    ) -> Dict[str, Any]:
        body = {"content": {"raw": content}}
        path = "/repositories/{}/{}/pullrequests/{}/comments".format(
            self.workspace, repo_slug, pr_id
        )
        if before_request is not None:
            before_request()
        return self._request_json("POST", self.base_url + path, body)

    def list_pull_request_comments(
        self,
        repo_slug: str,
        pr_id: int,
        before_request: Optional[Callable[[], None]] = None,
    ) -> List[Dict[str, Any]]:
        fields = ",".join(
            [
                "values.id",
                "values.parent.id",
                "values.content.raw",
                "values.created_on",
                "values.updated_on",
                "values.deleted",
                "values.resolution",
                "values.resolved",
                "values.outdated",
                "values.parent.id",
                "values.inline",
                "values.user.account_id",
                "values.user.display_name",
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
        visited: Set[str] = set()
        while url:
            if url in visited:
                raise BitbucketError("Bitbucket comment pagination repeated a page", retryable=True)
            visited.add(url)
            if before_request is not None:
                before_request()
            payload = self._request_json("GET", url)
            values = payload.get("values")
            if not isinstance(values, list) or any(not isinstance(item, dict) or type(item.get("id")) is not int for item in values):
                raise BitbucketError("Bitbucket comments response is incomplete", retryable=True)
            if payload.get("next") is not None and not isinstance(payload["next"], str):
                raise BitbucketError("Bitbucket comment pagination is invalid", retryable=True)
            comments.extend(values)
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
        line_side: str = "NEW",
    ) -> Dict[str, Any]:
        line_key = {"NEW": "to", "OLD": "from"}.get(line_side)
        if line_key is None:
            raise BitbucketError("inline comment line_side must be NEW or OLD")
        body = {
            "content": {"raw": content},
            "inline": {"path": path, line_key: line},
        }
        request_path = "/repositories/{}/{}/pullrequests/{}/comments".format(
            self.workspace, repo_slug, pr_id
        )
        if before_request is not None:
            before_request()
        return self._request_json("POST", self.base_url + request_path, body)

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
            state=item.get("state") or "OPEN",
        )

    def _request_json(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization_header(),
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

    def _authorization_header(self) -> str:
        if self.credentials.auth_type == "basic":
            return "Basic {}".format(self._basic_auth_token())
        if self.credentials.auth_type == "oauth_client_credentials":
            return "Bearer {}".format(self._oauth_access_token())
        raise BitbucketError("unsupported Bitbucket auth type: {}".format(self.credentials.auth_type), retryable=False)

    def _oauth_access_token(self) -> str:
        now = time.time()
        if self._oauth_token is not None and self._oauth_token.expires_at - 60 > now:
            return self._oauth_token.access_token

        data = urlencode({"grant_type": "client_credentials"}).encode("utf-8")
        raw_credentials = "{}:{}".format(
            self.credentials.oauth_client_id,
            self.credentials.oauth_client_secret,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic {}".format(base64.b64encode(raw_credentials).decode("ascii")),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "scout",
        }
        request = Request(self.credentials.oauth_token_url, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            exc.close()
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            raise BitbucketError(
                "Bitbucket OAuth token request failed with HTTP {}".format(exc.code),
                retryable=retryable,
            ) from exc
        except URLError as exc:
            raise BitbucketError("Bitbucket OAuth token request failed: {}".format(exc), retryable=True) from exc
        if not raw:
            raise BitbucketError("Bitbucket OAuth token request returned an empty response", retryable=True)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BitbucketError("Bitbucket OAuth token request returned invalid JSON", retryable=True) from exc
        if not isinstance(parsed, dict):
            raise BitbucketError("Bitbucket OAuth token request returned unexpected JSON", retryable=True)
        access_token = parsed.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise BitbucketError("Bitbucket OAuth token response did not include access_token", retryable=True)
        expires_in = parsed.get("expires_in", 3600)
        try:
            expires_in_seconds = int(expires_in)
        except (TypeError, ValueError):
            expires_in_seconds = 3600
        self._oauth_token = _OAuthToken(
            access_token=access_token,
            expires_at=now + max(expires_in_seconds, 0),
        )
        return access_token


def _quote_external_id(external_id: str) -> str:
    return quote(external_id, safe="")
