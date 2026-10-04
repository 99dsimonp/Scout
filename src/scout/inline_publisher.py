"""Deliver persisted round selections without repeating expensive provider reviews."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .bitbucket import BitbucketError
from .config import ConfigError
from .deduplication import SelectionFinding
from .schema import _format_inline_comment, _provider_label, to_round_notice

LOG = logging.getLogger(__name__)
_MARKER_TEMPLATE = "<!-- scout-publication:{} -->"
_INELIGIBLE = {"superseded", "cancelled", "review_failed"}


class PublicationHalted(RuntimeError):
    pass


def author_ids(comment: dict) -> set:
    user = comment.get("user") or {}
    return {str(user[key]) for key in ("account_id", "uuid") if user.get(key)}


def comment_eligible(comment: dict) -> bool:
    """Missing API state must never turn a possibly resolved issue into coverage."""
    inline = comment.get("inline") or {}
    line = inline.get("to") if inline.get("to") is not None else inline.get("from")
    resolution_known = "resolution" in comment or "resolved" in comment
    resolved = bool(comment.get("resolution")) or comment.get("resolved") is True
    anchor_current = comment.get("outdated") is False or inline.get("outdated") is False
    return (
        comment.get("deleted") is False and resolution_known and not resolved
        and anchor_current and bool(inline.get("path"))
        and isinstance(line, int)
        and not comment.get("parent")
    )


def _seconds_since(value: Any, now: float) -> float:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return now - value
    return now - datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _finding_payload(finding: SelectionFinding) -> dict:
    return {"id": finding.id, "annotation": finding.annotation, "rendered_content": finding.rendered_content,
            "source_commit": finding.source_commit, "merge_base": finding.merge_base,
            "provider": finding.provider, "historical": finding.historical}


def make_candidates(round_record: dict) -> List[SelectionFinding]:
    candidates = []
    for outcome in sorted(round_record["outcomes"], key=lambda item: item["provider"]):
        if outcome["status"] != "succeeded":
            continue
        result = outcome["result"] or {}
        for annotation in result.get("annotations", []):
            # Reserve the longest severity/footer before selection so coverage cannot
            # depend on text later removed to make room for a publication marker.
            reserved = dict(annotation, severity="CRITICAL")
            rendered = _format_inline_comment(reserved, _provider_label(outcome["provider"]), "", "", _MARKER_TEMPLATE.format("0" * 64))
            candidates.append(SelectionFinding(
                id="{}:{}".format(outcome["provider"], annotation["external_id"]),
                annotation=annotation, rendered_content=rendered.rsplit("\n\nScout:", 1)[0],
                source_commit=round_record["source_commit_hash"], merge_base=round_record.get("merge_base_hash") or "",
                provider=outcome["provider"],
            ))
    return sorted(candidates, key=lambda item: item.id)


class InlinePublisher:
    def __init__(self, state, bitbucket, config):
        self.state = state.inline
        self.bitbucket = bitbucket
        self.config = config
        self.identity: Optional[str] = None
        self.current_identities = set()
        self.trusted_identities = set()
        self.halted = False

    def initialize_identity(self) -> None:
        configured = self.config.bitbucket.bot_account_id
        discovered = set()
        try:
            discovered = author_ids({"user": self.bitbucket.current_user()})
        except BitbucketError as exc:
            LOG.warning("Bitbucket identity discovery unavailable: %s", exc)
        if configured and discovered and configured not in discovered:
            raise ConfigError("bitbucket.bot_account_id conflicts with the authenticated Bitbucket account")
        self.identity = configured or (sorted(discovered)[0] if discovered else None)
        if not self.identity:
            if len(self.config.agents.providers) > 1:
                raise ConfigError("multi-provider inline reviews require bitbucket.bot_account_id or GET /user identity")
            LOG.warning("No immutable Bitbucket identity: inline deduplication and marker recovery disabled; retries may duplicate comments")
            return
        self.current_identities = discovered | {self.identity}
        for identity in discovered | {self.identity}:
            self.state.remember_identity(identity, "discovered" if identity in discovered else "configured")
        self.trusted_identities = set(self.state.verified_identities())
        for intent in self.state.list_intents(statuses=["published"]):
            if intent.get("error") == "post_author_mismatch" and intent.get("author_id") not in self.current_identities:
                raise ConfigError("publication {} has an unresolved POST author mismatch".format(intent["id"]))

    def history(self, round_record: dict, before_request=None) -> List[SelectionFinding]:
        if not self.identity:
            return []
        try:
            comments = self.bitbucket.list_pull_request_comments(round_record["repo_slug"], round_record["pr_id"], before_request=before_request)
        except BitbucketError:
            LOG.warning("Comment eligibility unavailable for PR %s; retaining new findings", round_record["pr_id"])
            return []
        self._record_marker_anomalies(round_record, comments)
        for intent in self.state.list_intents(workspace=round_record["workspace"], repo_slug=round_record["repo_slug"],
                                             pr_id=round_record["pr_id"], statuses=["published"]):
            self._confirm(intent)
        known = {str(item["comment_id"]): item for item in self.state.history(round_record["workspace"], round_record["repo_slug"], round_record["pr_id"])}
        findings = []
        for comment in comments:
            if not author_ids(comment) & self.trusted_identities or not comment_eligible(comment):
                continue
            raw = (comment.get("content") or {}).get("raw", "")
            stored = known.get(str(comment["id"]))
            if stored:
                payload = stored["finding"]
                # A human edit removes the evidence used to establish coverage.
                expected = (stored.get("metadata") or {}).get("content")
                if expected is not None and expected != raw:
                    continue
                findings.append(SelectionFinding(**dict(payload, id="history:{}".format(comment["id"]), historical=True)))
                continue
            match = re.search(r"\n\nScout: (Critical|High|Medium|Low) issue found by ([^.]+)\.", raw)
            if not match:
                continue
            inline = comment["inline"]
            new_side = inline.get("to") is not None
            body = raw[:match.start()]
            annotation = {"path": inline["path"], "line": inline["to"] if new_side else inline["from"],
                          "line_side": "NEW" if new_side else "OLD", "severity": match.group(1).upper(),
                          "summary": body.splitlines()[0] if body else "", "details": body, "smallest_fix": "",
                          "reviewer": "general", "confidence": "MEDIUM"}
            finding = SelectionFinding(id="history:{}".format(comment["id"]), annotation=annotation,
                                       rendered_content=body, historical=True, provider=match.group(2))
            self.state.upsert_history(round_record["workspace"], round_record["repo_slug"], round_record["pr_id"], comment["id"], _finding_payload(finding), content=raw, legacy=True)
            findings.append(finding)
        eligible = {finding.id for finding in findings}
        superseded = set()
        for comment_id, stored in known.items():
            if "history:" + comment_id in eligible:
                superseded.update((stored.get("metadata") or {}).get("supersedes", []))
        return [finding for finding in findings if finding.id not in superseded]

    def _record_marker_anomalies(self, record: dict, comments: list) -> None:
        intents = self.state.list_intents(workspace=record["workspace"], repo_slug=record["repo_slug"],
                                          pr_id=record["pr_id"], statuses=["published", "cancelled"])
        for intent in intents:
            matches = {str(comment["id"]) for comment in comments
                       if intent["marker"] in (comment.get("content") or {}).get("raw", "")
                       and author_ids(comment) & self.trusted_identities}
            if matches - {str(value) for value in intent["comment_ids"]}:
                reason = "late_remote_comment" if intent["status"] == "cancelled" else "multiple_remote_comments"
                self.state.record_anomaly(intent["id"], sorted(matches),
                                          intent.get("error") if intent.get("error") == "post_author_mismatch" else reason)
                LOG.error("Publication %s: %s (%s)", intent["id"], reason, sorted(matches))

    def close_pr(self, workspace: str, repo_slug: str, pr_id: int, before_request=None) -> None:
        """A closed PR gets one lookup, never a resend, before local retention ends."""
        self.reconcile({"workspace": workspace, "repo_slug": repo_slug, "pr_id": pr_id},
                       before_request, allow_resend=False)
        self.state.finish_closed_pr(workspace, repo_slug, pr_id)

    def reconcile(self, round_record: dict, before_request=None, allow_resend: bool = True,
                  current_snapshot: Optional[Callable] = None) -> bool:
        intents = self.state.list_intents(workspace=round_record["workspace"], repo_slug=round_record["repo_slug"], pr_id=round_record["pr_id"], statuses=["unknown", "sending"])
        if not intents:
            return True
        comments = None
        lookup_started_at = time.time()
        if self.identity:
            try:
                comments = self.bitbucket.list_pull_request_comments(round_record["repo_slug"], round_record["pr_id"], before_request=before_request)
            except BitbucketError:
                return False
            self._record_marker_anomalies(round_record, comments)
        all_settled = True
        for intent in intents:
            if intent["status"] == "sending":
                # The caller owns this PR's publication lock; no local POST is active.
                intent = self.state.transition_intent(intent["id"], intent["version"], "unknown")
                if intent is None:
                    all_settled = False
                    continue
            matches = [c for c in comments or [] if intent["marker"] in (c.get("content") or {}).get("raw", "") and author_ids(c) & self.trusted_identities]
            if matches:
                confirmed = self.state.transition_intent(intent["id"], intent["version"], "published", comment_ids=[c["id"] for c in matches])
                if confirmed:
                    self._confirm(confirmed)
                else:
                    all_settled = False
                if len(matches) > 1:
                    LOG.error("Multiple remote comments carry publication marker %s", intent["id"])
                continue
            if not allow_resend:
                all_settled = False
                continue
            # Earlier pages can miss a POST even if pagination finishes after
            # the settling deadline; only a lookup begun after it permits resend.
            if _seconds_since(intent.get("last_attempt_at"), lookup_started_at) < self.config.queue.publication_settle_seconds:
                all_settled = False
                continue
            owner = self.state.get_round(intent["round_id"])
            if not owner or owner["status"] in _INELIGIBLE:
                # Absence after a timeout does not prove a superseded POST cannot arrive.
                all_settled = False
                continue
            if intent["kind"] == "finding":
                if current_snapshot is None:
                    all_settled = False
                    continue
                snapshot = current_snapshot()
                if snapshot.state != "OPEN":
                    all_settled = False
                    continue
            if intent["attempts"] >= self.config.queue.publication_max_attempts:
                LOG.error("PR %s blocked by unresolved publication %s; operator resolution required", round_record["pr_id"], intent["id"])
                all_settled = False
                continue
            LOG.warning("Best-effort resend of publication %s after complete negative lookup; a delayed original POST may duplicate it", intent["id"])
            if not self.state.transition_intent(intent["id"], intent["version"], "ready"):
                all_settled = False
        return all_settled

    def _reserve(self, round_record: dict, key: str, payload: dict, kind: str) -> dict:
        marker = _MARKER_TEMPLATE.format(hashlib.sha256((round_record["id"] + ":" + key).encode()).hexdigest())
        payload = dict(payload, marker=marker, content=payload["content"] + "\n" + marker)
        return self.state.reserve_intent(round_record["id"], key, payload, kind=kind)

    def _confirm(self, intent: dict) -> None:
        payload = intent["payload"]
        if intent["kind"] != "finding":
            return
        owner = self.state.get_round(intent["round_id"])
        finding = payload["finding"]
        self.state.mark_candidate(intent["round_id"], finding["id"], "published")
        for candidate_id in payload.get("covers", []):
            self.state.mark_candidate(intent["round_id"], candidate_id, "covered", target_id=finding["id"])
        for comment_id in intent["comment_ids"]:
            self.state.upsert_history(owner["workspace"], owner["repo_slug"], owner["pr_id"], comment_id, finding,
                                      content=payload["content"], round_id=owner["id"], author_id=intent.get("author_id"),
                                      supersedes=payload.get("supersedes", []))

    def publish_round(self, round_id: str, lease_token: str, current_snapshot: Callable, before_request=None) -> str:
        if self.halted:
            return "publication_failed"
        record = self.state.get_round(round_id)
        if not record or record["status"] in _INELIGIBLE:
            return "cancelled"
        if not self.reconcile(record, before_request, current_snapshot=current_snapshot):
            unresolved = self.state.list_intents(round_id=round_id, statuses=["unknown"])
            if any(intent["attempts"] >= self.config.queue.publication_max_attempts for intent in unresolved):
                return "publication_failed"
            return "publishing"
        plan = self.state.get_plan(round_id)
        if plan is None:
            return "ready_for_selection"
        candidates = {finding.id: finding for finding in make_candidates(record)}
        eligible_history = {finding.id for finding in self.history(record, before_request)}
        retained = set(plan["retained_ids"])
        covers = {}
        for decision in plan["decisions"]:
            if decision["decision"] != "covered":
                continue
            target = decision["covered_by"]
            if target.startswith("history:"):
                if target in eligible_history:
                    self.state.mark_candidate(round_id, decision["candidate_id"], "covered", target_id=target)
                else:
                    # Add an intent, never replace the saved selection or its wording.
                    retained.add(decision["candidate_id"])
            else:
                covers.setdefault(target, []).append(decision["candidate_id"])
        if candidates and any(item["status"] == "failed" for item in record["outcomes"]):
            self._reserve(record, "coverage_notice", {"content": to_round_notice(record, "coverage_notice")}, "coverage_notice")
        for candidate_id in sorted(retained):
            finding = candidates[candidate_id]
            annotation = dict(finding.annotation, severity=plan.get("severities", {}).get(candidate_id, finding.annotation["severity"]))
            footer = _format_inline_comment(annotation, _provider_label(finding.provider), "", "").rsplit("\n\nScout:", 1)[1]
            published = SelectionFinding(**dict(_finding_payload(finding), annotation=annotation))
            self._reserve(record, candidate_id, {"content": finding.rendered_content + "\n\nScout:" + footer,
                          "path": annotation["path"], "line": annotation["line"], "line_side": annotation["line_side"],
                          "candidate_id": candidate_id, "finding": _finding_payload(published), "covers": covers.get(candidate_id, []),
                          "supersedes": plan.get("superseded_history", {}).get(candidate_id, [])}, "finding")
        if not candidates:
            self._reserve(record, "clean_review", {"content": to_round_notice(record, "clean_review")}, "clean_review")
        snapshot_cache = [None, 0.0]
        def snapshot():
            if snapshot_cache[0] is None or time.monotonic() - snapshot_cache[1] >= self.config.queue.publication_snapshot_cache_seconds:
                snapshot_cache[:] = [current_snapshot(), time.monotonic()]
            return snapshot_cache[0]
        return self._deliver(record, lease_token, snapshot, before_request)

    def _deliver(self, record: dict, lease_token: str, current_snapshot: Callable, before_request=None) -> str:
        intents = self.state.list_intents(round_id=record["id"])
        priority = {"coverage_notice": 0, "finding": 1, "clean_review": 2}
        for intent in sorted(intents, key=lambda item: (priority[item["kind"]], item["id"])):
            if self.halted:
                return "publication_failed"
            if intent["status"] == "published":
                self._confirm(intent)
                continue
            if intent["status"] == "cancelled":
                continue
            if intent["status"] != "ready":
                return "publishing"
            if before_request:
                before_request()
            current = self.state.get_round(record["id"])
            if current["status"] in _INELIGIBLE or current.get("lease_token") != lease_token:
                return "cancelled"
            payload = intent["payload"]
            snapshot = current_snapshot()
            if self.halted:
                return "publication_failed"
            if snapshot.state != "OPEN":
                return "cancelled"
            if intent["attempts"] >= self.config.queue.publication_max_attempts:
                return "publication_failed"
            sending = self.state.transition_intent(intent["id"], intent["version"], "sending", lease_token=lease_token)
            if sending is None:
                return "publishing"
            try:
                def check_send():
                    if self.halted:
                        raise PublicationHalted("publication halted after a POST author mismatch")
                    if before_request:
                        before_request()
                    if self.halted:
                        raise PublicationHalted("publication halted after a POST author mismatch")
                check_send()
                if intent["kind"] == "finding":
                    response = self.bitbucket.publish_inline_pull_request_comment(record["repo_slug"], record["pr_id"], payload["path"], payload["line"], payload["content"], line_side=payload["line_side"], before_request=check_send)
                else:
                    response = self.bitbucket.publish_pull_request_comment(record["repo_slug"], record["pr_id"], payload["content"], before_request=check_send)
                if not response or not isinstance(response.get("id"), int):
                    raise BitbucketError("Comment POST response missing created comment ID", retryable=True)
            except Exception as exc:
                self.state.transition_intent(intent["id"], sending["version"], "unknown", error=str(exc))
                LOG.warning("Publication %s has an unknown POST outcome: %s", intent["id"], exc)
                return "publication_failed" if self.halted else "publishing"
            identities = author_ids(response)
            mismatch = bool(self.identity and not identities & self.current_identities)
            if mismatch:
                self.halted = True
            confirmed = self.state.transition_intent(intent["id"], sending["version"], "published", comment_ids=[response["id"]],
                        author_id=sorted(identities)[0] if identities else None, error="post_author_mismatch" if mismatch else None)
            if confirmed:
                self._confirm(confirmed)
            if mismatch:
                LOG.error("Comment %s created by unexpected author; publication halted", response["id"])
                return "publication_failed"
            if confirmed is None:
                self.state.record_anomaly(intent["id"], [response["id"]], "successful_post_after_state_change")
                return "publishing"
        return "completed"
