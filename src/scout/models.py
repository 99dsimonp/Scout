from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PullRequest:
    workspace: str
    repo_slug: str
    pr_id: int
    title: str
    description: str
    source_branch: str
    source_commit_hash: str
    destination_branch: str
    destination_commit_hash: Optional[str] = None
    merge_base_hash: Optional[str] = None
    is_draft: bool = False


def review_key(
    pr: PullRequest,
    policy_version: str,
    schema_version: str,
    provider: str,
    output_mode: str = "reports",
) -> str:
    payload = _review_identity_payload(pr, policy_version, schema_version, provider, output_mode)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_identity_payload(
    pr: PullRequest,
    policy_version: str,
    schema_version: str,
    provider: str,
    output_mode: str,
) -> dict:
    payload = {
        "workspace": pr.workspace,
        "repo_slug": pr.repo_slug,
        "pr_id": pr.pr_id,
        "policy_version": policy_version,
        "schema_version": schema_version,
        "provider": provider,
        "output_mode": output_mode,
    }
    if output_mode == "inline_comments":
        return payload
    payload.update(
        {
            "source_commit_hash": pr.source_commit_hash,
            "destination_branch": pr.destination_branch,
            "destination_commit_hash": pr.destination_commit_hash or "",
            "merge_base_hash": pr.merge_base_hash or "",
        }
    )
    return payload
