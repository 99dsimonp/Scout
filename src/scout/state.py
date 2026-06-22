from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from .models import PullRequest, review_key


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ReviewJob:
    id: int
    workspace: str
    repo_slug: str
    pr_id: int
    title: str
    description: str
    source_branch: str
    target_source_commit_hash: str
    running_source_commit_hash: Optional[str]
    destination_branch: str
    destination_commit_hash: Optional[str]
    merge_base_hash: Optional[str]
    reviewer_policy_version: str
    schema_version: str
    provider: str
    status: str
    superseded: bool
    attempts: int
    leased_until: Optional[str]
    lease_token: Optional[str]
    target_review_key: str
    running_review_key: Optional[str]
    target_review_run_id: str
    running_review_run_id: Optional[str]
    error_message: Optional[str]
    output_mode: str = "reports"


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("pragma journal_mode=WAL")
            conn.execute("pragma foreign_keys=ON")
            conn.executescript(
                """
                create table if not exists repositories (
                  id integer primary key,
                  workspace text not null,
                  repo_slug text not null,
                  clone_url text not null,
                  enabled integer not null default 1,
                  unique(workspace, repo_slug)
                );

                create table if not exists pull_request_state (
                  id integer primary key,
                  workspace text not null,
                  repo_slug text not null,
                  pr_id integer not null,
                  title text,
                  description text,
                  source_branch text,
                  destination_branch text,
                  source_commit_hash text,
                  destination_commit_hash text,
                  merge_base_hash text,
                  last_reviewed_commit_hash text,
                  last_review_key text,
                  last_seen_updated_on text,
                  review_status text,
                  last_report_id text,
                  updated_at text not null,
                  unique(workspace, repo_slug, pr_id)
                );

                create table if not exists review_jobs (
                  id integer primary key,
                  workspace text not null,
                  repo_slug text not null,
                  pr_id integer not null,
                  title text,
                  description text,
                  source_branch text,
                  target_source_commit_hash text not null,
                  running_source_commit_hash text,
                  destination_branch text,
                  destination_commit_hash text,
                  merge_base_hash text,
                  reviewer_policy_version text not null,
                  schema_version text not null,
                  provider text not null,
                  output_mode text not null default 'reports',
                  status text not null,
                  superseded integer not null default 0,
                  attempts integer not null default 0,
                  leased_until text,
                  lease_token text,
                  target_review_key text not null,
                  running_review_key text,
                  target_review_run_id text not null,
                  running_review_run_id text,
                  error_message text,
                  created_at text not null,
                  updated_at text not null,
                  unique(workspace, repo_slug, pr_id, reviewer_policy_version, schema_version, provider, output_mode)
                );

                create table if not exists provider_state (
                  provider text primary key,
                  status text not null,
                  cooldown_until text,
                  last_error text,
                  updated_at text not null
                );

                create table if not exists report_bootstrap_attempts (
                  workspace text not null,
                  repo_slug text not null,
                  pr_id integer not null,
                  provider text not null,
                  review_key text not null,
                  attempted_at text not null,
                  error_message text,
                  primary key(workspace, repo_slug, pr_id, provider, review_key)
                );

                create table if not exists processed_pr_comments (
                  workspace text not null,
                  repo_slug text not null,
                  pr_id integer not null,
                  comment_id text not null,
                  updated_on text not null,
                  review_requested integer not null,
                  processed_at text not null,
                  primary key(workspace, repo_slug, pr_id, comment_id, updated_on)
                );

                create table if not exists inline_comment_publications (
                  workspace text not null,
                  repo_slug text not null,
                  pr_id integer not null,
                  provider text not null,
                  output_mode text not null,
                  review_run_id text not null,
                  external_id text not null,
                  published_at text not null,
                  primary key(workspace, repo_slug, pr_id, provider, output_mode, review_run_id, external_id)
                );
                """
            )
            self._ensure_column(conn, "review_jobs", "lease_token", "text")
            self._ensure_column(conn, "review_jobs", "output_mode", "text not null default 'reports'")
            self._ensure_column(conn, "review_jobs", "target_review_run_id", "text")
            self._ensure_column(conn, "review_jobs", "running_review_run_id", "text")
            self._backfill_review_job_run_ids(conn)
            self._migrate_review_jobs_output_mode_unique(conn)
            self._migrate_failed_permanent_jobs(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_repository(self, workspace: str, repo_slug: str, clone_url: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into repositories(workspace, repo_slug, clone_url, enabled)
                values(?, ?, ?, 1)
                on conflict(workspace, repo_slug) do update set
                  clone_url=excluded.clone_url,
                  enabled=1
                """,
                (workspace, repo_slug, clone_url),
            )

    def enqueue_or_update_pr(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        provider: str,
        last_seen_updated_on: Optional[str] = None,
        output_mode: str = "reports",
    ) -> bool:
        key = review_key(pr, policy_version, schema_version, provider, output_mode)
        now = utcnow()
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into pull_request_state(
                  workspace, repo_slug, pr_id, title, description, source_branch,
                  destination_branch, source_commit_hash, destination_commit_hash,
                  merge_base_hash, last_seen_updated_on, review_status, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(workspace, repo_slug, pr_id) do update set
                  title=excluded.title,
                  description=excluded.description,
                  source_branch=excluded.source_branch,
                  destination_branch=excluded.destination_branch,
                  source_commit_hash=excluded.source_commit_hash,
                  destination_commit_hash=excluded.destination_commit_hash,
                  merge_base_hash=excluded.merge_base_hash,
                  last_seen_updated_on=excluded.last_seen_updated_on,
                  updated_at=excluded.updated_at
                """,
                (
                    pr.workspace,
                    pr.repo_slug,
                    pr.pr_id,
                    pr.title,
                    pr.description,
                    pr.source_branch,
                    pr.destination_branch,
                    pr.source_commit_hash,
                    pr.destination_commit_hash,
                    pr.merge_base_hash,
                    last_seen_updated_on,
                    "seen",
                    now,
                ),
            )
            state = conn.execute(
                """
                select last_review_key from pull_request_state
                where workspace=? and repo_slug=? and pr_id=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id),
            ).fetchone()

            existing = conn.execute(
                """
                select id, status, target_review_key from review_jobs
                where workspace=? and repo_slug=? and pr_id=?
                  and reviewer_policy_version=? and schema_version=? and provider=? and output_mode=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id, policy_version, schema_version, provider, output_mode),
            ).fetchone()
            if existing is None and state and state["last_review_key"] == key:
                return False
            if existing is None:
                run_id = uuid.uuid4().hex
                conn.execute(
                    """
                    insert into review_jobs(
                      workspace, repo_slug, pr_id, title, description,
                      source_branch, target_source_commit_hash, destination_branch, destination_commit_hash,
                      merge_base_hash, reviewer_policy_version, schema_version, provider,
                      output_mode, status, target_review_key, target_review_run_id, created_at, updated_at
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        pr.workspace,
                        pr.repo_slug,
                        pr.pr_id,
                        pr.title,
                        pr.description,
                        pr.source_branch,
                        pr.source_commit_hash,
                        pr.destination_branch,
                        pr.destination_commit_hash,
                        pr.merge_base_hash,
                        policy_version,
                        schema_version,
                        provider,
                        output_mode,
                        key,
                        run_id,
                        now,
                        now,
                    ),
                )
                return True

            if existing["target_review_key"] == key:
                conn.execute(
                    """
                    update review_jobs set
                      title=?,
                      description=?,
                      source_branch=?,
                      target_source_commit_hash=?,
                      destination_branch=?,
                      destination_commit_hash=?,
                      merge_base_hash=?
                    where id=?
                    """,
                    (
                        pr.title,
                        pr.description,
                        pr.source_branch,
                        pr.source_commit_hash,
                        pr.destination_branch,
                        pr.destination_commit_hash,
                        pr.merge_base_hash,
                        existing["id"],
                    ),
                )
                return False

            if existing["status"] == "publishing":
                return True

            run_id = uuid.uuid4().hex
            supersede = 1 if existing["status"] == "running" else 0
            next_status = existing["status"] if supersede else "pending"
            conn.execute(
                """
                update review_jobs set
                  title=?,
                  description=?,
                  source_branch=?,
                  target_source_commit_hash=?,
                  destination_branch=?,
                  destination_commit_hash=?,
                  merge_base_hash=?,
                  target_review_key=?,
                  target_review_run_id=?,
                  status=?,
                  superseded=?,
                  attempts=0,
                  error_message=null,
                  updated_at=?
                where id=?
                """,
                (
                    pr.title,
                    pr.description,
                    pr.source_branch,
                    pr.source_commit_hash,
                    pr.destination_branch,
                    pr.destination_commit_hash,
                    pr.merge_base_hash,
                    key,
                    run_id,
                    next_status,
                    supersede,
                    now,
                    existing["id"],
                ),
            )
            return True

    def force_enqueue_pr_review(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        provider: str,
        output_mode: str = "reports",
    ) -> bool:
        key = review_key(pr, policy_version, schema_version, provider, output_mode)
        run_id = uuid.uuid4().hex
        now = utcnow()
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into pull_request_state(
                  workspace, repo_slug, pr_id, title, description, source_branch,
                  destination_branch, source_commit_hash, destination_commit_hash,
                  merge_base_hash, last_seen_updated_on, review_status, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?, ?)
                on conflict(workspace, repo_slug, pr_id) do update set
                  title=excluded.title,
                  description=excluded.description,
                  source_branch=excluded.source_branch,
                  destination_branch=excluded.destination_branch,
                  source_commit_hash=excluded.source_commit_hash,
                  destination_commit_hash=excluded.destination_commit_hash,
                  merge_base_hash=excluded.merge_base_hash,
                  updated_at=excluded.updated_at
                """,
                (
                    pr.workspace,
                    pr.repo_slug,
                    pr.pr_id,
                    pr.title,
                    pr.description,
                    pr.source_branch,
                    pr.destination_branch,
                    pr.source_commit_hash,
                    pr.destination_commit_hash,
                    pr.merge_base_hash,
                    "seen",
                    now,
                ),
            )
            existing = conn.execute(
                """
                select id, status from review_jobs
                where workspace=? and repo_slug=? and pr_id=?
                  and reviewer_policy_version=? and schema_version=? and provider=? and output_mode=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id, policy_version, schema_version, provider, output_mode),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    insert into review_jobs(
                      workspace, repo_slug, pr_id, title, description,
                      source_branch, target_source_commit_hash, destination_branch, destination_commit_hash,
                      merge_base_hash, reviewer_policy_version, schema_version, provider,
                      output_mode, status, target_review_key, target_review_run_id, created_at, updated_at
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        pr.workspace,
                        pr.repo_slug,
                        pr.pr_id,
                        pr.title,
                        pr.description,
                        pr.source_branch,
                        pr.source_commit_hash,
                        pr.destination_branch,
                        pr.destination_commit_hash,
                        pr.merge_base_hash,
                        policy_version,
                        schema_version,
                        provider,
                        output_mode,
                        key,
                        run_id,
                        now,
                        now,
                    ),
                )
                return True

            if existing["status"] in ("running", "publishing"):
                conn.execute(
                    """
                    update review_jobs set
                      title=?,
                      description=?,
                      source_branch=?,
                      target_source_commit_hash=?,
                      destination_branch=?,
                      destination_commit_hash=?,
                      merge_base_hash=?,
                      target_review_key=?,
                      target_review_run_id=?,
                      superseded=1,
                      attempts=0,
                      error_message=null,
                      updated_at=?
                    where id=?
                    """,
                    (
                        pr.title,
                        pr.description,
                        pr.source_branch,
                        pr.source_commit_hash,
                        pr.destination_branch,
                        pr.destination_commit_hash,
                        pr.merge_base_hash,
                        key,
                        run_id,
                        now,
                        existing["id"],
                    ),
                )
                return True

            conn.execute(
                """
                update review_jobs set
                  title=?,
                  description=?,
                  source_branch=?,
                  target_source_commit_hash=?,
                  running_source_commit_hash=null,
                  destination_branch=?,
                  destination_commit_hash=?,
                  merge_base_hash=?,
                  target_review_key=?,
                  target_review_run_id=?,
                  running_review_key=null,
                  running_review_run_id=null,
                  status='pending',
                  superseded=0,
                  attempts=0,
                  leased_until=null,
                  lease_token=null,
                  error_message=null,
                  updated_at=?
                where id=?
                """,
                (
                    pr.title,
                    pr.description,
                    pr.source_branch,
                    pr.source_commit_hash,
                    pr.destination_branch,
                    pr.destination_commit_hash,
                    pr.merge_base_hash,
                    key,
                    run_id,
                    now,
                    existing["id"],
                ),
            )
            return True

    def has_review_for_key(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        provider: str,
        output_mode: str = "reports",
    ) -> bool:
        key = review_key(pr, policy_version, schema_version, provider, output_mode)
        with self.connect() as conn:
            job = conn.execute(
                """
                select 1 from review_jobs
                where workspace=? and repo_slug=? and pr_id=?
                  and reviewer_policy_version=? and schema_version=? and provider=? and output_mode=?
                  and target_review_key=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id, policy_version, schema_version, provider, output_mode, key),
            ).fetchone()
            if job is not None:
                return True
            state = conn.execute(
                """
                select 1 from pull_request_state
                where workspace=? and repo_slug=? and pr_id=? and last_review_key=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id, key),
            ).fetchone()
            return state is not None

    def should_bootstrap_report(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        provider: str,
        output_mode: str = "reports",
    ) -> bool:
        key = review_key(pr, policy_version, schema_version, provider, output_mode)
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from report_bootstrap_attempts
                where workspace=? and repo_slug=? and pr_id=? and provider=? and review_key=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id, provider, key),
            ).fetchone()
            return row is None

    def mark_report_bootstrap_attempted(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        provider: str,
        error_message: Optional[str] = None,
        output_mode: str = "reports",
    ) -> None:
        key = review_key(pr, policy_version, schema_version, provider, output_mode)
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                insert into report_bootstrap_attempts(
                  workspace, repo_slug, pr_id, provider, review_key, attempted_at, error_message
                )
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(workspace, repo_slug, pr_id, provider, review_key) do update set
                  attempted_at=excluded.attempted_at,
                  error_message=excluded.error_message
                """,
                (
                    pr.workspace,
                    pr.repo_slug,
                    pr.pr_id,
                    provider,
                    key,
                    now,
                    (error_message or "")[:2000],
                ),
            )

    def processed_pull_request_comment_review_requested(
        self,
        workspace: str,
        repo_slug: str,
        pr_id: int,
        comment_id: str,
        updated_on: str,
    ) -> Optional[bool]:
        with self.connect() as conn:
            row = conn.execute(
                """
                select review_requested from processed_pr_comments
                where workspace=? and repo_slug=? and pr_id=? and comment_id=? and updated_on=?
                """,
                (workspace, repo_slug, pr_id, str(comment_id), updated_on),
            ).fetchone()
            if row is None:
                return None
            return bool(row["review_requested"])

    def mark_pull_request_comment_processed(
        self,
        workspace: str,
        repo_slug: str,
        pr_id: int,
        comment_id: str,
        updated_on: str,
        review_requested: bool,
    ) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                insert into processed_pr_comments(
                  workspace, repo_slug, pr_id, comment_id, updated_on, review_requested, processed_at
                )
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(workspace, repo_slug, pr_id, comment_id, updated_on) do update set
                  review_requested=excluded.review_requested,
                  processed_at=excluded.processed_at
                """,
                (
                    workspace,
                    repo_slug,
                    pr_id,
                    str(comment_id),
                    updated_on,
                    1 if review_requested else 0,
                    now,
                ),
            )

    def inline_comment_published(
        self,
        job: ReviewJob,
        external_id: str,
    ) -> bool:
        review_run_id = job.running_review_run_id or job.target_review_run_id
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from inline_comment_publications
                where workspace=? and repo_slug=? and pr_id=? and provider=?
                  and output_mode=? and review_run_id=? and external_id=?
                """,
                (
                    job.workspace,
                    job.repo_slug,
                    job.pr_id,
                    job.provider,
                    job.output_mode,
                    review_run_id,
                    external_id,
                ),
            ).fetchone()
            return row is not None

    def mark_inline_comment_published(
        self,
        job: ReviewJob,
        external_id: str,
    ) -> None:
        review_run_id = job.running_review_run_id or job.target_review_run_id
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                insert into inline_comment_publications(
                  workspace, repo_slug, pr_id, provider, output_mode,
                  review_run_id, external_id, published_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(workspace, repo_slug, pr_id, provider, output_mode, review_run_id, external_id)
                do update set published_at=excluded.published_at
                """,
                (
                    job.workspace,
                    job.repo_slug,
                    job.pr_id,
                    job.provider,
                    job.output_mode,
                    review_run_id,
                    external_id,
                    now,
                ),
            )

    def prune_closed_pull_requests(self, workspace: str, repo_slug: str, open_pr_ids: Sequence[int]) -> int:
        open_ids = [int(pr_id) for pr_id in open_pr_ids]
        open_filter, params = _not_open_filter(open_ids)
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                delete from processed_pr_comments
                where workspace=?
                  and repo_slug=?
                  and {open_filter}
                  and not exists (
                    select 1 from review_jobs
                    where review_jobs.workspace=processed_pr_comments.workspace
                      and review_jobs.repo_slug=processed_pr_comments.repo_slug
                      and review_jobs.pr_id=processed_pr_comments.pr_id
                      and review_jobs.status in ('running', 'publishing')
                  )
                """.format(open_filter=open_filter),
                (workspace, repo_slug, *params),
            )
            conn.execute(
                """
                delete from inline_comment_publications
                where workspace=?
                  and repo_slug=?
                  and {open_filter}
                  and not exists (
                    select 1 from review_jobs
                    where review_jobs.workspace=inline_comment_publications.workspace
                      and review_jobs.repo_slug=inline_comment_publications.repo_slug
                      and review_jobs.pr_id=inline_comment_publications.pr_id
                      and review_jobs.status in ('running', 'publishing')
                  )
                """.format(open_filter=open_filter),
                (workspace, repo_slug, *params),
            )
            conn.execute(
                """
                delete from report_bootstrap_attempts
                where workspace=?
                  and repo_slug=?
                  and {open_filter}
                  and not exists (
                    select 1 from review_jobs
                    where review_jobs.workspace=report_bootstrap_attempts.workspace
                      and review_jobs.repo_slug=report_bootstrap_attempts.repo_slug
                      and review_jobs.pr_id=report_bootstrap_attempts.pr_id
                      and review_jobs.status in ('running', 'publishing')
                  )
                """.format(open_filter=open_filter),
                (workspace, repo_slug, *params),
            )
            conn.execute(
                """
                delete from review_jobs
                where workspace=?
                  and repo_slug=?
                  and {open_filter}
                  and not exists (
                    select 1 from review_jobs active
                    where active.workspace=review_jobs.workspace
                      and active.repo_slug=review_jobs.repo_slug
                      and active.pr_id=review_jobs.pr_id
                      and active.status in ('running', 'publishing')
                  )
                """.format(open_filter=open_filter),
                (workspace, repo_slug, *params),
            )
            result = conn.execute(
                """
                delete from pull_request_state
                where workspace=?
                  and repo_slug=?
                  and {open_filter}
                  and not exists (
                    select 1 from review_jobs
                    where review_jobs.workspace=pull_request_state.workspace
                      and review_jobs.repo_slug=pull_request_state.repo_slug
                      and review_jobs.pr_id=pull_request_state.pr_id
                      and review_jobs.status in ('running', 'publishing')
                  )
                """.format(open_filter=open_filter),
                (workspace, repo_slug, *params),
            )
            return int(result.rowcount or 0)

    def prune_ignored_pull_requests(
        self,
        workspace: str,
        repo_slug: str,
        ignored_pr_ids: Sequence[int],
        ignore_reason: Optional[str] = None,
    ) -> int:
        ignored_ids = [int(pr_id) for pr_id in ignored_pr_ids]
        if not ignored_ids:
            return 0
        ignored_filter, params = _in_filter(ignored_ids)
        reason = ignore_reason or "PR source branch is ignored by repository configuration"
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                delete from processed_pr_comments
                where workspace=?
                  and repo_slug=?
                  and {}
                """.format(ignored_filter),
                (workspace, repo_slug, *params),
            )
            conn.execute(
                """
                delete from inline_comment_publications
                where workspace=?
                  and repo_slug=?
                  and {}
                """.format(ignored_filter),
                (workspace, repo_slug, *params),
            )
            conn.execute(
                """
                delete from report_bootstrap_attempts
                where workspace=?
                  and repo_slug=?
                  and {}
                """.format(ignored_filter),
                (workspace, repo_slug, *params),
            )
            result = conn.execute(
                """
                delete from review_jobs
                where workspace=?
                  and repo_slug=?
                  and {}
                  and status in ('pending', 'failed_retryable', 'cancelled')
                """.format(ignored_filter),
                (workspace, repo_slug, *params),
            )
            active = conn.execute(
                """
                update review_jobs set
                  status='cancelled',
                  superseded=1,
                  error_message=?,
                  updated_at=?
                where workspace=?
                  and repo_slug=?
                  and {}
                  and status in ('running', 'publishing')
                """.format(ignored_filter),
                (reason[:2000], utcnow(), workspace, repo_slug, *params),
            )
            conn.execute(
                """
                delete from pull_request_state
                where workspace=?
                  and repo_slug=?
                  and {}
                  and not exists (
                    select 1 from review_jobs
                    where review_jobs.workspace=pull_request_state.workspace
                      and review_jobs.repo_slug=pull_request_state.repo_slug
                      and review_jobs.pr_id=pull_request_state.pr_id
                      and review_jobs.status in ('running', 'publishing')
                  )
                """.format(ignored_filter),
                (workspace, repo_slug, *params),
            )
            return int(result.rowcount or 0) + int(active.rowcount or 0)

    def seed_successful_review(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        provider: str,
        report_id: str,
        last_seen_updated_on: Optional[str] = None,
        output_mode: str = "reports",
    ) -> None:
        key = review_key(pr, policy_version, schema_version, provider, output_mode)
        run_id = uuid.uuid4().hex
        now = utcnow()
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into pull_request_state(
                  workspace, repo_slug, pr_id, title, description, source_branch,
                  destination_branch, source_commit_hash, destination_commit_hash,
                  merge_base_hash, last_reviewed_commit_hash, last_review_key,
                  last_seen_updated_on, review_status, last_report_id, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?)
                on conflict(workspace, repo_slug, pr_id) do update set
                  title=excluded.title,
                  description=excluded.description,
                  source_branch=excluded.source_branch,
                  destination_branch=excluded.destination_branch,
                  source_commit_hash=excluded.source_commit_hash,
                  destination_commit_hash=excluded.destination_commit_hash,
                  merge_base_hash=excluded.merge_base_hash,
                  last_reviewed_commit_hash=excluded.last_reviewed_commit_hash,
                  last_review_key=excluded.last_review_key,
                  last_seen_updated_on=excluded.last_seen_updated_on,
                  review_status=excluded.review_status,
                  last_report_id=excluded.last_report_id,
                  updated_at=excluded.updated_at
                """,
                (
                    pr.workspace,
                    pr.repo_slug,
                    pr.pr_id,
                    pr.title,
                    pr.description,
                    pr.source_branch,
                    pr.destination_branch,
                    pr.source_commit_hash,
                    pr.destination_commit_hash,
                    pr.merge_base_hash,
                    pr.source_commit_hash,
                    key,
                    last_seen_updated_on,
                    report_id,
                    now,
                ),
            )
            existing = conn.execute(
                """
                select id from review_jobs
                where workspace=? and repo_slug=? and pr_id=?
                  and reviewer_policy_version=? and schema_version=? and provider=? and output_mode=?
                """,
                (pr.workspace, pr.repo_slug, pr.pr_id, policy_version, schema_version, provider, output_mode),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    insert into review_jobs(
                      workspace, repo_slug, pr_id, title, description,
                      source_branch, target_source_commit_hash, running_source_commit_hash,
                      destination_branch, destination_commit_hash, merge_base_hash,
                      reviewer_policy_version, schema_version, provider, output_mode, status,
                      target_review_key, running_review_key, target_review_run_id,
                      running_review_run_id, created_at, updated_at
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pr.workspace,
                        pr.repo_slug,
                        pr.pr_id,
                        pr.title,
                        pr.description,
                        pr.source_branch,
                        pr.source_commit_hash,
                        pr.source_commit_hash,
                        pr.destination_branch,
                        pr.destination_commit_hash,
                        pr.merge_base_hash,
                        policy_version,
                        schema_version,
                        provider,
                        output_mode,
                        key,
                        key,
                        run_id,
                        run_id,
                        now,
                        now,
                    ),
                )
                return

            conn.execute(
                """
                update review_jobs set
                  title=?,
                  description=?,
                  source_branch=?,
                  target_source_commit_hash=?,
                  running_source_commit_hash=?,
                  destination_branch=?,
                  destination_commit_hash=?,
                  merge_base_hash=?,
                  target_review_key=?,
                  running_review_key=?,
                  target_review_run_id=?,
                  running_review_run_id=?,
                  status='succeeded',
                  superseded=0,
                  attempts=0,
                  leased_until=null,
                  lease_token=null,
                  error_message=null,
                  updated_at=?
                where id=?
                """,
                (
                    pr.title,
                    pr.description,
                    pr.source_branch,
                    pr.source_commit_hash,
                    pr.source_commit_hash,
                    pr.destination_branch,
                    pr.destination_commit_hash,
                    pr.merge_base_hash,
                    key,
                    key,
                    run_id,
                    run_id,
                    now,
                    existing["id"],
                ),
            )

    def mark_provider_cooldown(
        self,
        provider: str,
        error: str,
        cooldown_seconds: int,
        status: str = "quota_exhausted",
    ) -> str:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        cooldown_until = (now_dt + timedelta(seconds=cooldown_seconds)).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                insert into provider_state(provider, status, cooldown_until, last_error, updated_at)
                values(?, ?, ?, ?, ?)
                on conflict(provider) do update set
                  status=excluded.status,
                  cooldown_until=excluded.cooldown_until,
                  last_error=excluded.last_error,
                  updated_at=excluded.updated_at
                """,
                (provider, status, cooldown_until, error[:2000], now),
            )
        return cooldown_until

    def recover_abandoned_jobs(self, error_message: str = "Scout restarted while this job was active") -> int:
        now = utcnow()
        with self.connect() as conn:
            conn.execute("begin immediate")
            result = conn.execute(
                """
                update review_jobs set
                  status='pending',
                  superseded=0,
                  running_source_commit_hash=null,
                  running_review_key=null,
                  running_review_run_id=null,
                  leased_until=null,
                  lease_token=null,
                  error_message=?,
                  updated_at=?
                where status in ('running', 'publishing')
                """,
                (error_message[:2000], now),
            )
            return int(result.rowcount or 0)

    def get_active_provider_cooldown(self, provider: str) -> Optional[str]:
        now = utcnow()
        with self.connect() as conn:
            _clear_expired_provider_cooldowns(conn, now)
            row = conn.execute(
                """
                select cooldown_until from provider_state
                where provider=?
                  and status in ('rate_limited', 'quota_exhausted')
                  and cooldown_until is not null
                  and cooldown_until > ?
                """,
                (provider, now),
            ).fetchone()
            return row["cooldown_until"] if row else None

    def claim_pending_jobs(
        self,
        limit: int,
        lease_seconds: int,
        provider: Optional[str] = None,
    ) -> List[ReviewJob]:
        now = utcnow()
        leased_until = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=lease_seconds)
        ).isoformat()
        jobs: List[ReviewJob] = []
        provider_filter = ""
        provider_params = ()
        if provider is not None:
            provider_filter = "review_jobs.provider=? and"
            provider_params = (provider,)
        with self.connect() as conn:
            conn.execute("begin immediate")
            _clear_expired_provider_cooldowns(conn, now)
            rows = conn.execute(
                """
                select * from review_jobs
                where {} (
                  status='pending'
                   or (
                     status='failed_retryable'
                     and (leased_until is null or leased_until <= ?)
                   )
                   or (
                     status in ('running', 'publishing')
                     and leased_until is not null
                     and leased_until <= ?
                   )
                )
                and not exists (
                  select 1 from provider_state
                  where provider_state.provider=review_jobs.provider
                    and provider_state.status in ('rate_limited', 'quota_exhausted')
                    and provider_state.cooldown_until is not null
                    and provider_state.cooldown_until > ?
                )
                order by
                  case when status='failed_retryable' then 1 else 0 end asc,
                  case when status='failed_retryable' then updated_at else created_at end asc,
                  id asc
                limit ?
                """.format(provider_filter),
                (*provider_params, now, now, now, limit),
            ).fetchall()
            for row in rows:
                lease_token = uuid.uuid4().hex
                result = conn.execute(
                    """
                    update review_jobs set
                      status='running',
                      running_source_commit_hash=target_source_commit_hash,
                      running_review_key=target_review_key,
                      running_review_run_id=target_review_run_id,
                      superseded=0,
                      attempts=attempts + 1,
                      leased_until=?,
                      lease_token=?,
                      error_message=null,
                      updated_at=?
                    where id=? and (
                      status='pending'
                      or (
                        status='failed_retryable'
                        and (leased_until is null or leased_until <= ?)
                      )
                      or (
                        status in ('running', 'publishing')
                        and leased_until is not null
                        and leased_until <= ?
                      )
                    )
                    and not exists (
                      select 1 from provider_state
                      where provider_state.provider=review_jobs.provider
                        and provider_state.status in ('rate_limited', 'quota_exhausted')
                        and provider_state.cooldown_until is not null
                        and provider_state.cooldown_until > ?
                    )
                    """,
                    (leased_until, lease_token, now, row["id"], now, now, now),
                )
                if result.rowcount != 1:
                    continue
                updated = conn.execute("select * from review_jobs where id=?", (row["id"],)).fetchone()
                if updated is not None:
                    jobs.append(_job_from_row(updated))
        return jobs

    def claim_next_pending_job(self, lease_seconds_by_provider: Dict[str, int]) -> Optional[ReviewJob]:
        if not lease_seconds_by_provider:
            return None
        providers = tuple(lease_seconds_by_provider.keys())
        placeholders = ",".join("?" for _ in providers)
        now = utcnow()
        with self.connect() as conn:
            conn.execute("begin immediate")
            _clear_expired_provider_cooldowns(conn, now)
            row = conn.execute(
                """
                select * from review_jobs
                where provider in ({}) and (
                  review_jobs.status='pending'
                   or (
                     review_jobs.status='failed_retryable'
                     and (review_jobs.leased_until is null or review_jobs.leased_until <= ?)
                   )
                   or (
                     review_jobs.status in ('running', 'publishing')
                     and review_jobs.leased_until is not null
                     and review_jobs.leased_until <= ?
                   )
                )
                and not exists (
                  select 1 from provider_state
                  where provider_state.provider=review_jobs.provider
                    and provider_state.status in ('rate_limited', 'quota_exhausted')
                    and provider_state.cooldown_until is not null
                    and provider_state.cooldown_until > ?
                )
                order by
                  case when review_jobs.status='failed_retryable' then 1 else 0 end asc,
                  case
                    when review_jobs.status='failed_retryable' then review_jobs.updated_at
                    else review_jobs.created_at
                  end asc,
                  review_jobs.id asc
                limit 1
                """.format(placeholders),
                (*providers, now, now, now),
            ).fetchone()
            if row is None:
                return None
            lease_seconds = lease_seconds_by_provider[row["provider"]]
            leased_until = (
                datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=lease_seconds)
            ).isoformat()
            lease_token = uuid.uuid4().hex
            result = conn.execute(
                """
                update review_jobs set
                  status='running',
                  running_source_commit_hash=target_source_commit_hash,
                  running_review_key=target_review_key,
                  running_review_run_id=target_review_run_id,
                  superseded=0,
                  attempts=attempts + 1,
                  leased_until=?,
                  lease_token=?,
                  error_message=null,
                  updated_at=?
                where id=? and (
                  status='pending'
                  or (
                    status='failed_retryable'
                    and (leased_until is null or leased_until <= ?)
                  )
                  or (
                    status in ('running', 'publishing')
                    and leased_until is not null
                    and leased_until <= ?
                  )
                )
                and not exists (
                  select 1 from provider_state
                  where provider_state.provider=review_jobs.provider
                    and provider_state.status in ('rate_limited', 'quota_exhausted')
                    and provider_state.cooldown_until is not null
                    and provider_state.cooldown_until > ?
                )
                """,
                (leased_until, lease_token, now, row["id"], now, now, now),
            )
            if result.rowcount != 1:
                return None
            updated = conn.execute("select * from review_jobs where id=?", (row["id"],)).fetchone()
            return _job_from_row(updated) if updated else None

    def get_job(self, job_id: int) -> Optional[ReviewJob]:
        with self.connect() as conn:
            row = conn.execute("select * from review_jobs where id=?", (job_id,)).fetchone()
            return _job_from_row(row) if row else None

    def is_job_superseded(self, job_id: int, lease_token: Optional[str] = None) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return True
        if lease_token is not None and job.lease_token != lease_token:
            return True
        return bool(
            job.superseded
            or job.running_review_key != job.target_review_key
            or job.running_review_run_id != job.target_review_run_id
        )

    def mark_publishing(self, job: ReviewJob, lease_seconds: int) -> bool:
        now = utcnow()
        leased_until = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self.connect() as conn:
            result = conn.execute(
                """
                update review_jobs set
                  status='publishing',
                  leased_until=?,
                  updated_at=?
                where id=?
                  and status='running'
                  and superseded=0
                  and lease_token=?
                  and running_review_key=?
                  and target_review_key=?
                  and running_review_run_id=?
                  and target_review_run_id=?
                """,
                (
                    leased_until,
                    now,
                    job.id,
                    job.lease_token,
                    job.running_review_key,
                    job.running_review_key,
                    job.running_review_run_id,
                    job.running_review_run_id,
                ),
            )
            return result.rowcount == 1

    def renew_publishing_lease(self, job: ReviewJob, lease_seconds: int) -> bool:
        now = utcnow()
        leased_until = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self.connect() as conn:
            result = conn.execute(
                """
                update review_jobs set
                  leased_until=?,
                  updated_at=?
                where id=?
                  and status='publishing'
                  and superseded=0
                  and lease_token=?
                  and running_review_key=?
                  and target_review_key=?
                  and running_review_run_id=?
                  and target_review_run_id=?
                """,
                (
                    leased_until,
                    now,
                    job.id,
                    job.lease_token,
                    job.running_review_key,
                    job.running_review_key,
                    job.running_review_run_id,
                    job.running_review_run_id,
                ),
            )
            return result.rowcount == 1

    def mark_success(self, job: ReviewJob, report_id: str) -> bool:
        now = utcnow()
        with self.connect() as conn:
            result = conn.execute(
                """
                update review_jobs set
                  status='succeeded',
                  superseded=0,
                  leased_until=null,
                  lease_token=null,
                  error_message=null,
                  updated_at=?
                where id=?
                  and status='publishing'
                  and superseded=0
                  and lease_token=?
                  and running_review_key=?
                  and target_review_key=?
                  and running_review_run_id=?
                  and target_review_run_id=?
                """,
                (
                    now,
                    job.id,
                    job.lease_token,
                    job.running_review_key,
                    job.running_review_key,
                    job.running_review_run_id,
                    job.running_review_run_id,
                ),
            )
            if result.rowcount != 1:
                return False
            conn.execute(
                """
                update pull_request_state set
                  last_reviewed_commit_hash=?,
                  last_review_key=?,
                  review_status='succeeded',
                  last_report_id=?,
                  updated_at=?
                where workspace=? and repo_slug=? and pr_id=?
                """,
                (
                    job.running_source_commit_hash,
                    job.running_review_key,
                    report_id,
                    now,
                    job.workspace,
                    job.repo_slug,
                    job.pr_id,
                ),
            )
            return True

    def mark_retryable_failure(
        self,
        job_id: int,
        error: str,
        max_attempts: int,
        lease_token: Optional[str] = None,
        running_review_key: Optional[str] = None,
        retry_backoff_seconds: int = 0,
    ) -> bool:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self.connect() as conn:
            row = conn.execute("select attempts from review_jobs where id=?", (job_id,)).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            backoff = max(0, retry_backoff_seconds) * max(1, min(attempts, max_attempts))
            retry_after = (now_dt + timedelta(seconds=backoff)).isoformat() if backoff else None
            ownership_filter, params = _ownership_filter(lease_token, running_review_key)
            result = conn.execute(
                """
                update review_jobs set
                  status='failed_retryable',
                  leased_until=?,
                  lease_token=null,
                  error_message=?,
                  updated_at=?
                where id=?
                {}
                """.format(ownership_filter),
                (retry_after, error[:2000], now, job_id, *params),
            )
            return result.rowcount == 1

    def defer_job_for_provider_cooldown(
        self,
        job_id: int,
        error: str,
        lease_token: Optional[str] = None,
        running_review_key: Optional[str] = None,
    ) -> bool:
        now = utcnow()
        with self.connect() as conn:
            ownership_filter, params = _ownership_filter(lease_token, running_review_key)
            result = conn.execute(
                """
                update review_jobs set
                  status='failed_retryable',
                  attempts=case when attempts > 0 then attempts - 1 else attempts end,
                  leased_until=null,
                  lease_token=null,
                  error_message=?,
                  updated_at=?
                where id=?
                {}
                """.format(ownership_filter),
                (error[:2000], now, job_id, *params),
            )
            return result.rowcount == 1

    def return_superseded_to_pending(self, job_id: int, lease_token: Optional[str] = None) -> None:
        now = utcnow()
        with self.connect() as conn:
            lease_filter, params = _lease_filter(lease_token)
            conn.execute(
                """
                update review_jobs set
                  status='pending',
                  superseded=0,
                  running_source_commit_hash=null,
                  running_review_key=null,
                  running_review_run_id=null,
                  leased_until=null,
                  lease_token=null,
                  error_message=null,
                  updated_at=?
                where id=?
                {}
                  and status != 'cancelled'
                """.format(lease_filter),
                (now, job_id, *params),
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute("pragma table_info({})".format(table)).fetchall()}
        if column not in existing:
            conn.execute("alter table {} add column {} {}".format(table, column, definition))

    def _backfill_review_job_run_ids(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select id, target_review_run_id, running_review_run_id
            from review_jobs
            where target_review_run_id is null
               or target_review_run_id=''
               or (running_review_key is not null and (running_review_run_id is null or running_review_run_id=''))
            """
        ).fetchall()
        for row in rows:
            target_run_id = row["target_review_run_id"] or uuid.uuid4().hex
            running_run_id = row["running_review_run_id"]
            if row["running_review_run_id"] in (None, ""):
                running_run_id = target_run_id
            conn.execute(
                """
                update review_jobs set
                  target_review_run_id=?,
                  running_review_run_id=case
                    when running_review_key is not null then ?
                    else running_review_run_id
                  end
                where id=?
                """,
                (target_run_id, running_run_id, row["id"]),
            )

    def _migrate_review_jobs_output_mode_unique(self, conn: sqlite3.Connection) -> None:
        expected = [
            "workspace",
            "repo_slug",
            "pr_id",
            "reviewer_policy_version",
            "schema_version",
            "provider",
            "output_mode",
        ]
        for index in conn.execute("pragma index_list(review_jobs)").fetchall():
            if not bool(index["unique"]):
                continue
            columns = [
                row["name"]
                for row in conn.execute("pragma index_info({})".format(index["name"])).fetchall()
            ]
            if columns == expected:
                return

        conn.executescript(
            """
            create table review_jobs_new (
              id integer primary key,
              workspace text not null,
              repo_slug text not null,
              pr_id integer not null,
              title text,
              description text,
              source_branch text,
              target_source_commit_hash text not null,
              running_source_commit_hash text,
              destination_branch text,
              destination_commit_hash text,
              merge_base_hash text,
              reviewer_policy_version text not null,
              schema_version text not null,
              provider text not null,
              output_mode text not null default 'reports',
              status text not null,
              superseded integer not null default 0,
              attempts integer not null default 0,
              leased_until text,
              lease_token text,
              target_review_key text not null,
              running_review_key text,
              target_review_run_id text not null,
              running_review_run_id text,
              error_message text,
              created_at text not null,
              updated_at text not null,
              unique(workspace, repo_slug, pr_id, reviewer_policy_version, schema_version, provider, output_mode)
            );

            insert into review_jobs_new(
              id, workspace, repo_slug, pr_id, title, description, source_branch,
              target_source_commit_hash, running_source_commit_hash, destination_branch,
              destination_commit_hash, merge_base_hash, reviewer_policy_version,
              schema_version, provider, output_mode, status, superseded, attempts,
              leased_until, lease_token, target_review_key, running_review_key,
              target_review_run_id, running_review_run_id, error_message,
              created_at, updated_at
            )
            select
              id, workspace, repo_slug, pr_id, title, description, source_branch,
              target_source_commit_hash, running_source_commit_hash, destination_branch,
              destination_commit_hash, merge_base_hash, reviewer_policy_version,
              schema_version, provider, coalesce(output_mode, 'reports'), status,
              superseded, attempts, leased_until, lease_token, target_review_key,
              running_review_key, target_review_run_id, running_review_run_id,
              error_message, created_at, updated_at
            from review_jobs;

            drop table review_jobs;
            alter table review_jobs_new rename to review_jobs;
            """
        )

    def _migrate_failed_permanent_jobs(self, conn: sqlite3.Connection) -> None:
        now = utcnow()
        conn.execute(
            """
            update review_jobs set
              status='failed_retryable',
              leased_until=null,
              lease_token=null,
              updated_at=?
            where status='failed_permanent'
            """,
            (now,),
        )


def _job_from_row(row: sqlite3.Row) -> ReviewJob:
    return ReviewJob(
        id=int(row["id"]),
        workspace=row["workspace"],
        repo_slug=row["repo_slug"],
        pr_id=int(row["pr_id"]),
        title=row["title"] or "",
        description=row["description"] or "",
        source_branch=row["source_branch"] or "",
        target_source_commit_hash=row["target_source_commit_hash"],
        running_source_commit_hash=row["running_source_commit_hash"],
        destination_branch=row["destination_branch"] or "",
        destination_commit_hash=row["destination_commit_hash"],
        merge_base_hash=row["merge_base_hash"],
        reviewer_policy_version=row["reviewer_policy_version"],
        schema_version=row["schema_version"],
        provider=row["provider"],
        output_mode=row["output_mode"],
        status=row["status"],
        superseded=bool(row["superseded"]),
        attempts=int(row["attempts"]),
        leased_until=row["leased_until"],
        lease_token=row["lease_token"],
        target_review_key=row["target_review_key"],
        running_review_key=row["running_review_key"],
        target_review_run_id=row["target_review_run_id"],
        running_review_run_id=row["running_review_run_id"],
        error_message=row["error_message"],
    )


def _clear_expired_provider_cooldowns(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(
        """
        update provider_state set
          status='available',
          cooldown_until=null,
          updated_at=?
        where status in ('rate_limited', 'quota_exhausted')
          and cooldown_until is not null
          and cooldown_until <= ?
        """,
        (now, now),
    )


def _lease_filter(lease_token: Optional[str]) -> tuple:
    if lease_token is None:
        return "", ()
    return "and lease_token=?", (lease_token,)


def _not_open_filter(open_pr_ids: Sequence[int]) -> tuple:
    if not open_pr_ids:
        return "1=1", ()
    placeholders = ",".join("?" for _ in open_pr_ids)
    return "pr_id not in ({})".format(placeholders), tuple(open_pr_ids)


def _in_filter(pr_ids: Sequence[int]) -> tuple:
    if not pr_ids:
        return "1=0", ()
    placeholders = ",".join("?" for _ in pr_ids)
    return "pr_id in ({})".format(placeholders), tuple(pr_ids)


def _ownership_filter(
    lease_token: Optional[str],
    running_review_key: Optional[str],
) -> tuple:
    clauses = []
    params = []
    if lease_token is not None:
        clauses.append("and lease_token=?")
        params.append(lease_token)
    if running_review_key is not None:
        clauses.append("and superseded=0")
        clauses.append("and running_review_key=?")
        clauses.append("and target_review_key=?")
        params.extend([running_review_key, running_review_key])
    return "\n                ".join(clauses), tuple(params)
