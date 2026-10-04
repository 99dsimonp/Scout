from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .bitbucket import BitbucketClient, BitbucketCredentials, BitbucketError
from .claude import ClaudeRunner
from .codex import CodexRunner
from .comment_request import CommentRequestValidationError, has_scout_mention
from .config import AppConfig, ConfigError, CredentialStore
from .gitops import GitError, GitManager
from .models import PullRequest
from .prompt import build_provider_prompt
from .provider import PROVIDER_COOLDOWN_STATUS, ProviderError, ProviderSuperseded
from .retention import cleanup_review_artifacts
from .review_plan import (
    DEFAULT_RISK,
    build_review_plan,
    effective_subagent_max_per_lens,
    normalize_risk,
)
from .runtime_lock import RuntimeLock
from .schema import (
    ReviewValidationError,
    ValidatedReview,
    parse_review_json,
    summarize_findings,
    to_bitbucket_annotations,
    to_pr_comments,
    to_bitbucket_report,
    filter_annotation_locations,
    validate_review_output,
)
from .state import ReviewJob, ReviewSnapshotError, StateStore, utcnow
from .usage import parse_provider_usage_from_logs

LOG = logging.getLogger(__name__)
_REVIEW_LOG_LOCK = threading.Lock()
_NO_FINDINGS_INLINE_COMMENT_ID = "__scout_no_findings__"
_RISK_CAPACITY_POLL_SECONDS = 1.0


class ScoutDaemon:
    def __init__(self, config: AppConfig, credentials: CredentialStore):
        self.config = config
        self.credentials = credentials
        self.state = StateStore(config.service.state_db)
        self.bitbucket = BitbucketClient(
            base_url=config.bitbucket.api_base_url,
            workspace=config.bitbucket.workspace,
            credentials=_bitbucket_credentials(config, credentials),
        )
        try:
            ssh_key_path = str(credentials.path(config.bitbucket.ssh_key_credential))
        except ConfigError:
            ssh_key_path = None
        self.git = GitManager(config.service.state_dir, ssh_key_path=ssh_key_path)
        self.provider_names = list(config.agents.providers)
        runtime_provider_names = list(self.provider_names)
        risk_config = getattr(config.review, "risk", None)
        risk_provider = getattr(risk_config, "provider", None) if getattr(risk_config, "enabled", True) else None
        if risk_provider and risk_provider not in runtime_provider_names:
            runtime_provider_names.append(risk_provider)
        request_comments_config = getattr(config.review, "request_comments", None)
        request_comments_provider = (
            getattr(request_comments_config, "provider", None)
            if getattr(config.review, "output_mode", "reports") == "inline_comments"
            else None
        )
        if request_comments_provider and request_comments_provider not in runtime_provider_names:
            runtime_provider_names.append(request_comments_provider)
        deduplication = getattr(config.review, "deduplication", None)
        if request_comments_provider and getattr(deduplication, "enabled", True):
            selection_provider = getattr(deduplication, "provider", request_comments_provider)
            if selection_provider not in runtime_provider_names:
                runtime_provider_names.append(selection_provider)
        self.provider_configs = {
            provider: _provider_config(config, provider)
            for provider in runtime_provider_names
        }
        self.providers = {
            provider: _provider_runner(config, credentials, provider)
            for provider in runtime_provider_names
        }
        self.max_parallel_reviews = config.queue.max_parallel_reviews
        self.clone_urls: Dict[str, str] = {
            repo.slug: repo.clone_url for repo in config.bitbucket.repositories
        }
        self.repository_configs = {
            repo.slug: repo for repo in config.bitbucket.repositories
        }
        self._risk_cache: Dict[tuple, str] = {}
        self._risk_cache_locks: Dict[tuple, threading.Lock] = {}
        self._risk_cache_guard = threading.Lock()
        self._provider_slots: Dict[str, threading.BoundedSemaphore] = {}
        self._provider_slots_guard = threading.Lock()
        self._ensure_provider_slots()

    def initialize(self) -> None:
        self.state.initialize()
        Path(self.config.service.state_dir).mkdir(parents=True, exist_ok=True)
        for repo in self.config.bitbucket.repositories:
            self.state.upsert_repository(
                self.config.bitbucket.workspace,
                repo.slug,
                repo.clone_url,
                enabled=getattr(repo, "review_enabled", True),
            )
        self.validate_startup()
        recovered = self.state.recover_abandoned_jobs()
        if recovered:
            LOG.info("recovered abandoned active review jobs count=%s", recovered)
        if getattr(getattr(self.config, "review", None), "output_mode", "reports") == "inline_comments":
            self._inline_dispatch().publisher.initialize_identity()
        self.cleanup_old_artifacts()

    def validate_startup(self) -> None:
        for repo in self.config.bitbucket.repositories:
            self.bitbucket.validate_repository(repo.slug)
            self.git.validate_clone_url(repo.clone_url)
        for name, provider in self.providers.items():
            try:
                provider.validate_startup()
            except ProviderError as exc:
                if getattr(self.config.review, "output_mode", "reports") != "inline_comments":
                    raise
                LOG.warning("inline review provider unavailable at startup provider=%s error=%s", name, exc)
                self.state.mark_provider_cooldown(
                    name, str(exc), getattr(exc, "cooldown_seconds", None) or _retry_backoff_seconds(self.config),
                    PROVIDER_COOLDOWN_STATUS,
                )

    def run_forever(self) -> None:
        with RuntimeLock(self.config.service.state_dir):
            self.initialize()
            with ThreadPoolExecutor(max_workers=self.max_parallel_reviews) as pool:
                futures = {}
                next_poll_at = 0.0
                while True:
                    now = time.monotonic()
                    if now >= next_poll_at:
                        self.poll_once()
                        next_poll_at = time.monotonic() + self.config.polling.interval_seconds
                        if not futures:
                            self.cleanup_old_artifacts()
                    self._schedule(pool, futures)
                    wait_timeout = min(5.0, _seconds_until_next_poll(next_poll_at, time.monotonic()))
                    if futures:
                        done, _ = wait(list(futures), timeout=wait_timeout, return_when=FIRST_COMPLETED)
                        _reap_worker_futures(done, futures)
                    elif wait_timeout > 0:
                        time.sleep(wait_timeout)

    def run_once(self) -> None:
        with RuntimeLock(self.config.service.state_dir):
            self.initialize()
            self.poll_once()
            self.run_pending_jobs()
            self.cleanup_old_artifacts()

    def run_pending_jobs(self) -> None:
        with ThreadPoolExecutor(max_workers=self.max_parallel_reviews) as pool:
            futures = {}
            self._schedule(pool, futures)
            while futures:
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                _reap_worker_futures(done, futures)
                self._schedule(pool, futures)

    def poll_once(self) -> None:
        if not self.config.polling.enabled:
            return
        for repo in self.config.bitbucket.repositories:
            if not getattr(repo, "review_enabled", True):
                continue
            LOG.info("polling repository workspace=%s repo=%s", self.config.bitbucket.workspace, repo.slug)
            try:
                prs = self.bitbucket.list_open_pull_requests(repo.slug)
            except BitbucketError as exc:
                LOG.error("Bitbucket poll failed repo=%s retryable=%s error=%s", repo.slug, exc.retryable, exc)
                continue
            all_open_pr_ids = [pr.pr_id for pr in prs]
            ignored_pr_ids = [pr.pr_id for pr in prs if self._is_ignored_source_branch(repo, pr.source_branch)]
            if ignored_pr_ids:
                ignored_set = set(ignored_pr_ids)
                ignored_count = self.state.prune_ignored_pull_requests(
                    self.config.bitbucket.workspace,
                    repo.slug,
                    ignored_pr_ids,
                )
                if ignored_count:
                    LOG.info(
                        "removed queued state for ignored source branch pull requests workspace=%s repo=%s count=%s",
                        self.config.bitbucket.workspace,
                        repo.slug,
                        ignored_count,
                    )
                prs = [pr for pr in prs if pr.pr_id not in ignored_set]
            ignored_target_pr_ids = [
                pr.pr_id for pr in prs if self._is_ignored_target_branch(repo, pr.destination_branch)
            ]
            if ignored_target_pr_ids:
                ignored_set = set(ignored_target_pr_ids)
                ignored_count = self.state.prune_ignored_pull_requests(
                    self.config.bitbucket.workspace,
                    repo.slug,
                    ignored_target_pr_ids,
                    "PR destination branch is ignored by repository configuration",
                )
                if ignored_count:
                    LOG.info(
                        "removed queued state for ignored target branch pull requests workspace=%s repo=%s count=%s",
                        self.config.bitbucket.workspace,
                        repo.slug,
                        ignored_count,
                    )
                prs = [pr for pr in prs if pr.pr_id not in ignored_set]
            output_mode = getattr(self.config.review, "output_mode", "reports")
            if output_mode == "inline_comments" or getattr(repo, "ignore_draft_pull_requests", False):
                ignored_draft_pr_ids = [pr.pr_id for pr in prs if pr.is_draft]
                if ignored_draft_pr_ids:
                    ignored_set = set(ignored_draft_pr_ids)
                    reason = (
                        "PR is draft and inline comment review mode only reviews non-draft pull requests"
                        if output_mode == "inline_comments"
                        else "PR is draft and repository is configured to ignore draft pull requests"
                    )
                    ignored_count = self.state.prune_ignored_pull_requests(
                        self.config.bitbucket.workspace,
                        repo.slug,
                        ignored_draft_pr_ids,
                        reason,
                    )
                    if ignored_count:
                        LOG.info(
                            "removed queued state for draft pull requests workspace=%s repo=%s count=%s",
                            self.config.bitbucket.workspace,
                            repo.slug,
                            ignored_count,
                        )
                    prs = [pr for pr in prs if pr.pr_id not in ignored_set]
            if repo.pr_ids:
                wanted = set(repo.pr_ids)
                prs = [pr for pr in prs if pr.pr_id in wanted]
            elif prs is not None:
                if output_mode == "inline_comments":
                    all_open_pr_ids = self._inline_dispatch().close_missing_prs(
                        self.config.bitbucket.workspace, repo.slug, all_open_pr_ids,
                    )
                pruned = self.state.prune_closed_pull_requests(
                    self.config.bitbucket.workspace,
                    repo.slug,
                    all_open_pr_ids,
                )
                if pruned:
                    LOG.info(
                        "pruned closed pull request state workspace=%s repo=%s count=%s",
                        self.config.bitbucket.workspace,
                        repo.slug,
                        pruned,
                    )
            for pr in prs:
                policy_version = self.config.review.policy_version
                schema_version = "v1"
                if output_mode == "inline_comments":
                    try:
                        self._queue_inline_round(pr, policy_version, schema_version)
                    except GitError as exc:
                        LOG.warning("cannot resolve inline snapshot repo=%s pr=%s error=%s", pr.repo_slug, pr.pr_id, exc)
                        continue
                    self._process_review_request_comments(pr, policy_version, schema_version, output_mode)
                    continue
                for provider in self.provider_names:
                    if (
                        output_mode == "reports"
                        and not self.state.has_review_for_key(
                            pr, policy_version, schema_version, provider, output_mode=output_mode
                        )
                        and self.state.should_bootstrap_report(
                            pr, policy_version, schema_version, provider, output_mode=output_mode
                        )
                    ):
                        seeded = self._seed_existing_provider_report(
                            pr,
                            provider,
                            policy_version,
                            schema_version,
                        )
                        if seeded:
                            continue
                    queued = self.state.enqueue_or_update_pr(
                        pr=pr,
                        policy_version=policy_version,
                        schema_version=schema_version,
                        provider=provider,
                        output_mode=output_mode,
                    )
                    if queued:
                        LOG.info(
                            "queued or updated review provider=%s repo=%s pr=%s commit=%s",
                            provider,
                            pr.repo_slug,
                            pr.pr_id,
                            pr.source_commit_hash,
                        )
                if output_mode == "inline_comments":
                    self._process_review_request_comments(pr, policy_version, schema_version, output_mode)

    def _process_review_request_comments(
        self,
        pr: PullRequest,
        policy_version: str,
        schema_version: str,
        output_mode: str,
    ) -> None:
        try:
            comments = self.bitbucket.list_pull_request_comments(pr.repo_slug, pr.pr_id)
        except BitbucketError as exc:
            LOG.warning(
                "Bitbucket comment poll failed repo=%s pr=%s retryable=%s error=%s",
                pr.repo_slug,
                pr.pr_id,
                exc.retryable,
                exc,
            )
            return

        for comment in comments:
            if comment.get("deleted") is True:
                continue
            comment_id = str(comment.get("id") or "")
            updated_on = str(comment.get("updated_on") or "")
            body = _comment_raw_content(comment)
            if not comment_id or not updated_on or not has_scout_mention(body):
                continue
            processed = self.state.processed_pull_request_comment_review_requested(
                pr.workspace,
                pr.repo_slug,
                pr.pr_id,
                comment_id,
                updated_on,
            )
            if processed is not None:
                continue
            try:
                classification = self._classify_review_request_comment(pr, comment_id, updated_on, body)
            except (ProviderError, ProviderSuperseded, CommentRequestValidationError) as exc:
                LOG.warning(
                    "review request comment classification failed repo=%s pr=%s comment=%s error=%s",
                    pr.repo_slug,
                    pr.pr_id,
                    comment_id,
                    exc,
                )
                continue
            except Exception as exc:
                LOG.warning(
                    "review request comment classification failed unexpectedly repo=%s pr=%s comment=%s error=%s",
                    pr.repo_slug,
                    pr.pr_id,
                    comment_id,
                    exc,
                )
                continue

            if not classification.review_requested:
                self.state.mark_pull_request_comment_processed(
                    pr.workspace,
                    pr.repo_slug,
                    pr.pr_id,
                    comment_id,
                    updated_on,
                    False,
                )
                LOG.info(
                    "ignored Scout mention that did not request review repo=%s pr=%s comment=%s reason=%s",
                    pr.repo_slug,
                    pr.pr_id,
                    comment_id,
                    classification.reason,
                )
                continue

            try:
                snapshot = self._resolve_inline_snapshot(pr)
                self.state.inline.create_round(
                    snapshot, self.provider_names, policy_version, schema_version,
                    trigger="request", request_comment=(comment_id, updated_on),
                )
            except GitError as exc:
                LOG.warning("cannot resolve requested inline snapshot repo=%s pr=%s error=%s", pr.repo_slug, pr.pr_id, exc)

    def _resolve_inline_snapshot(self, pr: PullRequest) -> PullRequest:
        mirror = self.git.ensure_mirror(pr.workspace, pr.repo_slug, self.clone_urls[pr.repo_slug])
        return self.git.resolve_review_snapshot(mirror, pr)

    def _queue_inline_round(self, pr: PullRequest, policy_version: str, schema_version: str) -> None:
        rounds = self.state.inline.list_rounds(workspace=pr.workspace, repo_slug=pr.repo_slug, pr_id=pr.pr_id)
        if rounds or self.state.inline.has_pre_round_review(pr):
            return
        self.state.inline.create_round(self._resolve_inline_snapshot(pr), self.provider_names, policy_version, schema_version)

    def _inline_pr_eligible(self, pr: PullRequest) -> bool:
        repo = self.repository_configs[pr.repo_slug]
        return (pr.state == "OPEN" and not pr.is_draft and getattr(repo, "review_enabled", True)
                and not self._is_ignored_source_branch(repo, pr.source_branch)
                and not self._is_ignored_target_branch(repo, pr.destination_branch))

    def _inline_dispatch(self):
        if not hasattr(self, "_inline_dispatcher"):
            from .inline_dispatch import InlineDispatch
            self._inline_dispatcher = InlineDispatch(self)
        return self._inline_dispatcher

    def _classify_review_request_comment(
        self,
        pr: PullRequest,
        comment_id: str,
        updated_on: str,
        body: str,
    ):
        request_config = self.config.review.request_comments
        provider = request_config.provider
        runner = self.providers.get(provider)
        if runner is None:
            raise ProviderError("review request provider is not available: {}".format(provider), retryable=True)
        cooldown_until = self.state.get_active_provider_cooldown(provider)
        if cooldown_until is not None:
            raise ProviderError(
                "review request provider {} is in cooldown until {}".format(provider, cooldown_until),
                retryable=True,
            )
        if not self._acquire_provider_slot(provider, blocking=False):
            raise ProviderError("review request provider capacity unavailable: {}".format(provider), retryable=True)

        run_dir = str(
            Path(self.config.service.state_dir)
            / "runs"
            / "comment-requests"
            / _safe_path_segment(pr.repo_slug)
            / str(pr.pr_id)
            / "{}-{}".format(_safe_path_segment(comment_id), _safe_path_segment(updated_on))
        )
        try:
            if provider == "codex":
                return runner.classify_review_request(
                    comment=body,
                    model=request_config.model,
                    reasoning_effort=request_config.effort,
                    timeout_seconds=request_config.timeout_seconds,
                    run_dir=run_dir,
                    is_superseded=lambda: False,
                )
            if provider == "claude":
                return runner.classify_review_request(
                    comment=body,
                    model=request_config.model,
                    effort=request_config.effort,
                    timeout_seconds=request_config.timeout_seconds,
                    run_dir=run_dir,
                    is_superseded=lambda: False,
                )
            raise ProviderError("unsupported review request provider: {}".format(provider), retryable=False)
        except ProviderError as exc:
            provider_cooldown_seconds = getattr(exc, "cooldown_seconds", None)
            if provider_cooldown_seconds:
                provider_status = exc.provider_status or PROVIDER_COOLDOWN_STATUS
                cooldown_until = self.state.mark_provider_cooldown(
                    provider,
                    str(exc),
                    provider_cooldown_seconds,
                    provider_status,
                )
                LOG.warning(
                    "provider cooldown set from review request classification provider=%s status=%s until=%s",
                    provider,
                    provider_status,
                    cooldown_until,
                )
            raise
        finally:
            self._release_provider_slot(provider)

    def _is_ignored_source_branch(self, repo, source_branch: str) -> bool:
        for pattern in getattr(repo, "ignored_source_branches", ()):
            if re.search(pattern, source_branch):
                return True
        return False

    def _is_ignored_target_branch(self, repo, target_branch: str) -> bool:
        for pattern in getattr(repo, "ignored_target_branches", ()):
            if re.search(pattern, target_branch):
                return True
        return False

    def _seed_existing_provider_report(
        self,
        pr: PullRequest,
        provider: str,
        policy_version: str,
        schema_version: str,
    ) -> bool:
        report_id = self.config.reports.report_id_for(provider)
        try:
            exists = self.bitbucket.report_exists(pr.repo_slug, pr.source_commit_hash, report_id)
        except BitbucketError as exc:
            self.state.mark_report_bootstrap_attempted(
                pr,
                policy_version,
                schema_version,
                provider,
                str(exc),
            )
            LOG.warning(
                "Bitbucket report bootstrap failed provider=%s repo=%s pr=%s commit=%s retryable=%s error=%s",
                provider,
                pr.repo_slug,
                pr.pr_id,
                pr.source_commit_hash,
                exc.retryable,
                exc,
            )
            return False
        if not exists:
            self.state.mark_report_bootstrap_attempted(
                pr,
                policy_version,
                schema_version,
                provider,
            )
            return False
        self.state.seed_successful_review(
            pr=pr,
            policy_version=policy_version,
            schema_version=schema_version,
            provider=provider,
            report_id=report_id,
        )
        LOG.info(
            "seeded succeeded review from existing Bitbucket report provider=%s repo=%s pr=%s commit=%s report_id=%s",
            provider,
            pr.repo_slug,
            pr.pr_id,
            pr.source_commit_hash,
            report_id,
        )
        return True

    def _run_reserved_job(self, job: ReviewJob) -> None:
        self.run_job(job, provider_reserved=True)

    def run_job(self, job: ReviewJob, provider_reserved: bool = False) -> None:
        stop = threading.Event()
        lease_lost = threading.Event()
        lease_seconds = self._lease_seconds(job.provider)
        renewal_interval = _lease_renewal_interval(lease_seconds)
        next_renewal_delay = renewal_interval
        deadline = time.monotonic()
        try:
            deadline += max(0.0, (
                datetime.fromisoformat(job.leased_until) - datetime.now(timezone.utc)
            ).total_seconds())
        except (TypeError, ValueError):
            # A missing or invalid claim expiry gives no grace if renewal fails.
            pass

        def is_lease_lost() -> bool:
            # Provider polling must observe expiry even while renewal SQL blocks.
            if time.monotonic() >= deadline:
                lease_lost.set()
            return lease_lost.is_set()

        def renew() -> bool:
            nonlocal deadline, next_renewal_delay
            started_at = time.monotonic()
            try:
                renewed = self.state.renew_job_lease(job, lease_seconds)
            except Exception:
                LOG.exception("failed to renew review job lease id=%s", job.id)
                next_renewal_delay = min(1.0, renewal_interval)
            else:
                if not renewed:
                    lease_lost.set()
                else:
                    # StateStore timestamps the lease before SQL and truncates
                    # to whole seconds. SQL wait time must not extend our deadline.
                    deadline = started_at + lease_seconds - 1
                    next_renewal_delay = renewal_interval
            return not is_lease_lost()

        if not renew():
            self.state.return_superseded_to_pending(job.id, job.lease_token)
            if provider_reserved:
                self._release_provider_slot(job.provider)
            return

        def heartbeat() -> None:
            while not stop.wait(max(0.0, min(next_renewal_delay, deadline - time.monotonic()))):
                if is_lease_lost() or not renew():
                    return

        # Fetches and provider-slot waits can outlast the original lease before
        # a provider starts polling for supersession.
        worker = threading.Thread(target=heartbeat, name="review-lease-{}".format(job.id), daemon=True)
        worker.start()
        reservation = {"held": provider_reserved}
        try:
            self._run_job(job, is_lease_lost, reservation)
        finally:
            if reservation["held"]:
                self._release_provider_slot(job.provider)
            stop.set()
            worker.join()

    def _run_job(self, job: ReviewJob, is_lease_lost: Callable[[], bool], reservation: dict) -> None:
        def is_superseded() -> bool:
            return is_lease_lost() or self.state.is_job_superseded(job.id, job.lease_token)

        LOG.info("starting review job id=%s repo=%s pr=%s commit=%s", job.id, job.repo_slug, job.pr_id, job.running_source_commit_hash)
        mirror = None
        worktree = None
        related_worktrees: List[Tuple[object, object]] = []
        related_context: List[Dict[str, str]] = []
        run_dir = None
        source_commit = job.running_source_commit_hash or job.target_source_commit_hash
        usage_logged = False
        output_mode = job.output_mode
        replaying = False
        try:
            snapshot = self.state.load_review_snapshot(job)
            if snapshot is None:
                provider_config = self.provider_configs[job.provider]
                provider_runner = self.providers[job.provider]
                cooldown_until = self.state.get_active_provider_cooldown(job.provider)
                if cooldown_until is not None:
                    if self._record_inline_failure(job, ProviderError("provider cooldown"), cooldown=True):
                        return
                    LOG.info("provider cooldown active provider=%s until=%s job=%s", job.provider, cooldown_until, job.id)
                    deferred = self.state.defer_job_for_provider_cooldown(
                        job.id,
                        "provider {} is in cooldown until {}".format(job.provider, cooldown_until),
                        job.lease_token,
                        job.running_review_key,
                    )
                    if not deferred and self.state.is_job_superseded(job.id, job.lease_token):
                        self.state.return_superseded_to_pending(job.id, job.lease_token)
                    return
                pr = PullRequest(
                    workspace=job.workspace,
                    repo_slug=job.repo_slug,
                    pr_id=job.pr_id,
                    title=job.title,
                    description=job.description,
                    source_branch=job.source_branch,
                    source_commit_hash=source_commit,
                    destination_branch=job.destination_branch,
                    destination_commit_hash=job.destination_commit_hash,
                    merge_base_hash=job.merge_base_hash,
                )
                clone_url = self.clone_urls[job.repo_slug]
                mirror = self.git.ensure_mirror(job.workspace, job.repo_slug, clone_url)
                if is_superseded():
                    raise ProviderSuperseded("review superseded during fetch")
                worktree = self.git.create_worktree(mirror, pr, suffix="job-{}".format(job.id))
                repository_configs = getattr(self, "repository_configs", {})
                repo_config = repository_configs.get(job.repo_slug)
                for related_slug in getattr(repo_config, "related_repositories", ()):
                    related_config = repository_configs[related_slug]
                    related_mirror = self.git.ensure_mirror(
                        job.workspace,
                        related_slug,
                        related_config.clone_url,
                    )
                    resolved_ref, related_commit = self.git.resolve_context_revision(
                        related_mirror,
                        getattr(related_config, "context_ref", None),
                    )
                    related_worktree = self.git.create_context_worktree(
                        related_mirror,
                        job.workspace,
                        job.repo_slug,
                        related_slug,
                        related_commit,
                        job.id,
                    )
                    related_worktrees.append((related_mirror, related_worktree))
                    related_entry = {
                        "slug": related_slug,
                        "ref": resolved_ref,
                        "commit": related_commit,
                        "path": str(related_worktree),
                    }
                    related_context.append(related_entry)
                    LOG.info(
                        "prepared related repository context job=%s primary_repo=%s related_repo=%s ref=%s commit=%s path=%s",
                        job.id,
                        job.repo_slug,
                        related_slug,
                        resolved_ref,
                        related_commit,
                        related_worktree,
                    )
                def before_comments_request():
                    if is_superseded():
                        raise ProviderSuperseded("review superseded while loading PR comments")

                pull_request_comments = self.bitbucket.list_pull_request_comments(
                    job.repo_slug,
                    job.pr_id,
                    before_request=before_comments_request,
                )
                context = self.git.prepare_context(
                    mirror,
                    worktree,
                    pr,
                    related_repositories=related_context,
                    pull_request_comments=pull_request_comments,
                )
                if is_superseded():
                    raise ProviderSuperseded("review superseded during context preparation")
                risk = self._risk_for_job(
                    job, source_commit, is_superseded,
                    reserved_provider=job.provider if reservation["held"] else None,
                )
                effective_max_per_lens = effective_subagent_max_per_lens(
                    provider_config.subagent_max_per_lens,
                    provider_config.max_subagents,
                )
                review_plan = build_review_plan(
                    changed_lines=int(context["changed_lines"]),
                    description=pr.description,
                    small_loc_limit=provider_config.subagent_small_loc_limit,
                    medium_loc_limit=provider_config.subagent_medium_loc_limit,
                    large_loc_limit=provider_config.subagent_large_loc_limit,
                    high_risk_bonus=provider_config.subagent_high_risk_bonus,
                    max_subagents_per_lens=effective_max_per_lens,
                    risk=risk,
                )
                LOG.info(
                    "review plan job=%s changed_lines=%s risk=%s high_risk=%s subagents_per_lens=%s total_subagents=%s",
                    job.id,
                    review_plan.changed_lines,
                    review_plan.risk,
                    review_plan.high_risk,
                    review_plan.subagents_per_lens,
                    review_plan.total_subagents,
                )
                if review_plan.total_subagents > provider_config.max_subagents:
                    raise ProviderError(
                        "review plan requests {} subagents, exceeding agents.{}.max_subagents={}".format(
                            review_plan.total_subagents,
                            job.provider,
                            provider_config.max_subagents,
                        ),
                        retryable=False,
                    )
                prompt = build_provider_prompt(job.provider, context, self.config.review.schema_path, review_plan)
                run_dir = str(Path(self.config.service.state_dir) / "runs" / str(job.id))
                if not reservation["held"]:
                    self._acquire_provider_slot(job.provider, blocking=True)
                    reservation["held"] = True
                try:
                    if is_superseded():
                        raise ProviderSuperseded("review superseded while waiting for provider capacity")
                    provider_run_args = dict(
                        worktree=str(worktree),
                        prompt=prompt,
                        schema_path=self.config.review.schema_path,
                        run_dir=run_dir,
                        is_superseded=is_superseded,
                    )
                    if related_context:
                        provider_run_args["additional_dirs"] = [
                            entry["path"] for entry in related_context
                        ]
                    result = provider_runner.run(**provider_run_args)
                finally:
                    self._release_provider_slot(job.provider)
                    reservation["held"] = False
                _append_provider_usage_log_entry(
                    self.config.service.state_dir,
                    _provider_usage_log_entry(
                        job,
                        source_commit,
                        run_dir,
                        "provider_completed",
                        result.usage,
                        related_repositories=related_context,
                    ),
                )
                usage_logged = True
                if is_superseded():
                    raise ProviderSuperseded("review superseded before publish")
                parsed = parse_review_json(result.final_message)
                validated = validate_review_output(parsed, max_findings=self.config.review.max_findings)
                if validated.annotations:
                    diff = context.get("diff")
                    if not isinstance(diff, str):
                        diff = Path(str(context["diff_path"])).read_text(encoding="utf-8")
                    original_annotations = validated.annotations
                    allowed_line_sides = (
                        ("NEW", "OLD") if output_mode == "inline_comments" else ("NEW",)
                    )
                    validated = filter_annotation_locations(
                        validated,
                        diff,
                        allowed_line_sides=allowed_line_sides,
                        allow_old_dead_code=output_mode == "reports",
                    )
                    retained_external_ids = {
                        annotation["external_id"] for annotation in validated.annotations
                    }
                    discarded_annotations = [
                        annotation
                        for annotation in original_annotations
                        if annotation["external_id"] not in retained_external_ids
                    ]
                    for annotation in discarded_annotations:
                        LOG.warning(
                            "discarded finding without publishable annotation location "
                            "job=%s output_mode=%s external_id=%s path=%s line=%s side=%s",
                            job.id,
                            output_mode,
                            annotation["external_id"],
                            annotation["path"],
                            annotation["line"],
                            annotation["line_side"],
                        )
                if output_mode == "inline_comments":
                    _append_review_log_entry(self.config.service.state_dir, _review_log_entry(
                        job, source_commit, validated, run_dir, result.usage, related_repositories=related_context,
                    ))
                    if not self.state.inline.save_result(job, asdict(validated)):
                        raise ProviderSuperseded("inline round no longer accepts this result")
                    return
                snapshot = {
                    "version": 1,
                    "source_commit": source_commit,
                    "review": {
                        "recommendation": validated.recommendation,
                        "report": validated.report,
                        "annotations": validated.annotations,
                    },
                    "publication": self._review_publication(job, validated, source_commit),
                    "review_log": _review_log_entry(
                        job, source_commit, validated, run_dir, result.usage,
                        related_repositories=related_context,
                    ),
                }
                if not self.state.save_review_snapshot(job, snapshot):
                    raise ProviderSuperseded("review superseded before saving publication snapshot")
                review_log_path = _append_review_log_entry(
                    self.config.service.state_dir,
                    snapshot["review_log"],
                )
                LOG.info(
                    "appended review log path=%s job=%s repo=%s pr=%s",
                    review_log_path,
                    job.id,
                    job.repo_slug,
                    job.pr_id,
                )
            else:
                # Never regenerate a saved run, including when loading it fails.
                # The frozen payloads also preserve wording across config upgrades.
                if snapshot.get("version") != 1 or not isinstance(snapshot.get("publication"), dict):
                    raise ReviewSnapshotError("unsupported saved review snapshot for job {}".format(job.id))
                stored_review = snapshot["review"]
                validate_review_output(stored_review, max_findings=len(stored_review["annotations"]))
                source_commit = snapshot["source_commit"]
                usage_logged = True
                replaying = True
                LOG.info("resuming saved publication job=%s review_run=%s", job.id, job.running_review_run_id)
            if is_superseded() or not self.state.mark_publishing(job, self._lease_seconds(job.provider)):
                raise ProviderSuperseded("review superseded before publish")
            report_id = self._publish_review(job, snapshot["publication"], source_commit, replaying=replaying)
            if not self.state.mark_success(job, report_id):
                raise ProviderSuperseded("review superseded before success mark")
            LOG.info("review job succeeded id=%s repo=%s pr=%s", job.id, job.repo_slug, job.pr_id)
        except ProviderSuperseded as exc:
            LOG.info("review job superseded id=%s error=%s", job.id, exc)
            if run_dir is not None and not usage_logged:
                _append_provider_usage_log_entry(
                    self.config.service.state_dir,
                    _provider_usage_log_entry_from_logs(
                        job,
                        source_commit,
                        run_dir,
                        "superseded",
                        str(exc),
                        related_repositories=related_context,
                    ),
                )
            self.state.return_superseded_to_pending(job.id, job.lease_token)
        except (BitbucketError, GitError, ProviderError, ReviewValidationError, ReviewSnapshotError) as exc:
            retryable = getattr(exc, "retryable", True)
            LOG.error("review job failed id=%s retryable=%s error=%s", job.id, retryable, exc)
            if run_dir is not None and not usage_logged:
                _append_provider_usage_log_entry(
                    self.config.service.state_dir,
                    _provider_usage_log_entry_from_logs(
                        job,
                        source_commit,
                        run_dir,
                        "failed",
                        str(exc),
                        related_repositories=related_context,
                    ),
                )
            provider_cooldown_seconds = getattr(exc, "cooldown_seconds", None)
            if isinstance(exc, ProviderError) and provider_cooldown_seconds:
                provider_status = exc.provider_status or PROVIDER_COOLDOWN_STATUS
                cooldown_until = self.state.mark_provider_cooldown(
                    job.provider,
                    str(exc),
                    provider_cooldown_seconds,
                    provider_status,
                )
                LOG.warning(
                    "provider cooldown set provider=%s status=%s until=%s job=%s",
                    job.provider,
                    provider_status,
                    cooldown_until,
                    job.id,
                )
            if self.state.is_job_superseded(job.id, job.lease_token):
                self.state.return_superseded_to_pending(job.id, job.lease_token)
                return
            cooldown = bool(isinstance(exc, ProviderError) and provider_cooldown_seconds)
            if self._record_inline_failure(job, exc, cooldown=cooldown):
                return
            if cooldown:
                marked = self.state.defer_job_for_provider_cooldown(
                    job.id,
                    str(exc),
                    job.lease_token,
                    job.running_review_key,
                )
            else:
                marked = self.state.mark_retryable_failure(
                    job.id,
                    str(exc),
                    self.config.queue.max_attempts,
                    job.lease_token,
                    job.running_review_key,
                    _retry_backoff_seconds(self.config),
                )
            if not marked:
                self.state.return_superseded_to_pending(job.id, job.lease_token)
        except Exception as exc:
            LOG.exception("review job failed unexpectedly id=%s", job.id)
            if self.state.is_job_superseded(job.id, job.lease_token):
                self.state.return_superseded_to_pending(job.id, job.lease_token)
                return
            if self._record_inline_failure(job, exc):
                return
            marked = self.state.mark_retryable_failure(
                job.id,
                str(exc),
                self.config.queue.max_attempts,
                job.lease_token,
                job.running_review_key,
                _retry_backoff_seconds(self.config),
            )
            if not marked:
                self.state.return_superseded_to_pending(job.id, job.lease_token)
        finally:
            for related_mirror, related_worktree in reversed(related_worktrees):
                try:
                    self.git.remove_worktree(related_mirror, related_worktree)
                except Exception as exc:
                    LOG.warning(
                        "failed to remove related repository worktree path=%s error=%s",
                        related_worktree,
                        exc,
                    )
            if mirror is not None and worktree is not None:
                try:
                    self.git.remove_worktree(mirror, worktree)
                except Exception as exc:
                    LOG.warning("failed to remove worktree path=%s error=%s", worktree, exc)

    def _review_publication(self, job: ReviewJob, validated: ValidatedReview, source_commit: str) -> Dict[str, object]:
        return {
            "report_id": self.config.reports.report_id_for(job.provider),
            "report": to_bitbucket_report(
                validated, self.config.reports.title_for(job.provider), provider=job.provider,
                model_metadata=_provider_model_metadata(job.provider, self.provider_configs[job.provider]),
            ),
            "annotations": to_bitbucket_annotations(validated, provider=job.provider),
            "comments": to_pr_comments(
                validated, provider=job.provider, source_commit=source_commit,
                severities=_comment_severities(config=self.config),
            ),
        }

    def _publish_review(
        self, job: ReviewJob, publication: Dict[str, object], source_commit: str, replaying: bool = False,
    ) -> str:
        report_id = publication["report_id"]
        if job.output_mode == "inline_comments":
            if replaying:
                current_pr = self.bitbucket.get_pull_request(
                    job.repo_slug, job.pr_id,
                    before_request=lambda: self._renew_publish_or_superseded(job),
                )
                if not self._inline_pr_eligible(current_pr):
                    self.state.prune_ignored_pull_requests(job.workspace, job.repo_slug, [job.pr_id], "PR is no longer eligible")
                    raise ProviderSuperseded("PR is no longer eligible")
            no_findings_comment = publication["no_findings_comment"]
            if no_findings_comment and not self.state.inline_comment_published(
                job,
                _NO_FINDINGS_INLINE_COMMENT_ID,
            ):
                if _pull_request_comment_exists(
                    self.bitbucket,
                    job.repo_slug,
                    job.pr_id,
                    no_findings_comment,
                    before_request=lambda: self._renew_publish_or_superseded(job),
                ):
                    self.state.mark_inline_comment_published(job, _NO_FINDINGS_INLINE_COMMENT_ID)
                else:
                    if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
                        raise ProviderSuperseded("review superseded before no-findings comment publish")
                    self.bitbucket.publish_pull_request_comment(
                        job.repo_slug,
                        job.pr_id,
                        no_findings_comment,
                        before_request=lambda: self._renew_publish_or_superseded(job),
                    )
                    self.state.mark_inline_comment_published(job, _NO_FINDINGS_INLINE_COMMENT_ID)
            for comment in publication["comments"]:
                external_id = comment["external_id"]
                if self.state.inline_comment_published(job, external_id):
                    continue
                if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
                    raise ProviderSuperseded("review superseded before inline comment publish")
                self.bitbucket.publish_inline_pull_request_comment(
                    job.repo_slug,
                    job.pr_id,
                    comment["path"],
                    comment["line"],
                    comment["content"],
                    line_side=comment["line_side"],
                    before_request=lambda: self._renew_publish_or_superseded(job),
                )
                self.state.mark_inline_comment_published(job, external_id)
            return report_id
        if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
            raise ProviderSuperseded("review superseded before report publish")
        self.bitbucket.publish_report(job.repo_slug, source_commit, report_id, publication["report"])
        if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
            raise ProviderSuperseded("review superseded before annotation publish")
        self.bitbucket.publish_annotations(
            job.repo_slug,
            source_commit,
            report_id,
            publication["annotations"],
            before_request=lambda: self._renew_publish_or_superseded(job),
        )
        for pr_comment in publication["comments"]:
            # The publication ledger includes output_mode and review_run_id.
            # Fingerprint the frozen body; a fresh review run may repeat it.
            comment_id = "report-comment-" + hashlib.sha256(pr_comment.encode("utf-8")).hexdigest()
            if self.state.inline_comment_published(job, comment_id):
                continue
            if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
                raise ProviderSuperseded("review superseded before PR comment publish")
            self.bitbucket.publish_pull_request_comment(
                job.repo_slug,
                job.pr_id,
                pr_comment,
                before_request=lambda: self._renew_publish_or_superseded(job),
            )
            self.state.mark_inline_comment_published(job, comment_id)
        return report_id

    def _record_inline_failure(self, job: ReviewJob, error: Exception, cooldown: bool = False) -> bool:
        if job.output_mode != "inline_comments":
            return False
        round_ = self.state.inline.round_for_job(job)
        if round_ is None:
            return False
        recovery_seconds = getattr(self.config.queue, "max_provider_recovery_seconds", 3600)
        if cooldown:
            # Cooldown deferral does not consume an attempt, so skip the attempt limit.
            return self.state.inline.fail_or_recover_for_cooldown(
                round_["id"], job.provider, str(error), recovery_seconds,
            )
        self.state.inline.start_provider_recovery(round_["id"], job.provider, recovery_seconds)
        if not getattr(error, "retryable", True) or job.attempts >= self.config.queue.max_attempts:
            self.state.inline.fail_provider(round_["id"], job.provider, str(error))
            return True
        return False

    def _schedule(self, pool: ThreadPoolExecutor, futures: dict) -> None:
        self._worker_futures = futures
        inline = getattr(getattr(self.config, "review", None), "output_mode", "reports") == "inline_comments"
        if inline:
            self._inline_dispatch().maintain()
        capacity = self.max_parallel_reviews - len(futures)
        while capacity > 0:
            dispatched = False
            prefer_publication = getattr(self, "_prefer_publication", True)
            for publication in (prefer_publication, not prefer_publication):
                if publication:
                    dispatched = self._schedule_saved_publication(pool, futures)
                    if not dispatched and inline:
                        dispatched = self._inline_dispatch().schedule(pool, futures)
                else:
                    dispatched = self._schedule_review(pool, futures)
                if dispatched:
                    self._prefer_publication = not publication
                    break
            if not dispatched:
                return
            capacity -= 1

    def _schedule_saved_publication(self, pool, futures):
        job = self.state.claim_saved_publication_job(
            self.config.queue.job_timeout_seconds,
            excluded_job_ids=[_future_job_id(item) for item in futures.values() if _future_job_id(item) is not None],
        )
        if job is None:
            return False
        futures[pool.submit(self.run_job, job)] = {"id": job.id, "provider": "", "pr": (job.workspace, job.repo_slug, job.pr_id)}
        return True

    def _schedule_review(self, pool: ThreadPoolExecutor, futures: dict) -> bool:
        reservations = {}
        for provider in self.provider_names:
            if self.state.get_active_provider_cooldown(provider) is not None:
                continue
            if self._acquire_provider_slot(provider, blocking=False):
                reservations[provider] = self._lease_seconds(provider)
        if not reservations:
            return False
        job = None
        dispatched = False
        try:
            job = self.state.claim_next_pending_job(
                reservations,
                excluded_job_ids=[_future_job_id(item) for item in futures.values() if _future_job_id(item) is not None],
                exclude_saved_publications=True,
                # Inline jobs run only as round members; poll_once adopts old ones.
                require_inline_round=True,
            )
            if job is None:
                return False
            futures[pool.submit(self._run_reserved_job, job)] = {"id": job.id, "provider": job.provider, "pr": (job.workspace, job.repo_slug, job.pr_id)}
            dispatched = True
            return True
        finally:
            for provider in reservations:
                if not dispatched or provider != job.provider:
                    self._release_provider_slot(provider)

    def _renew_publish_or_superseded(self, job: ReviewJob) -> None:
        if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
            raise ProviderSuperseded("review superseded during publish")

    def _risk_for_job(
        self,
        job: ReviewJob,
        source_commit: str,
        is_superseded: Callable[[], bool],
        reserved_provider: Optional[str] = None,
    ) -> str:
        risk_config = getattr(self.config.review, "risk", None)
        if risk_config is None or not getattr(risk_config, "enabled", True):
            return DEFAULT_RISK
        self._ensure_risk_cache()
        key = _risk_cache_key(job, source_commit)
        provider = getattr(risk_config, "provider", "codex")
        # Take the risk provider slot before the per-snapshot lock. A job waiting
        # on the lock may hold that provider's slot, so the lock holder must not
        # wait for one. Jobs of the risk provider reuse their own slot.
        slot_held = provider == reserved_provider
        acquired = False
        while not slot_held:
            with self._risk_cache_guard:
                cached = self._risk_cache.get(key)
            if cached is not None:
                return cached
            if self.providers.get(provider) is None or self.state.get_active_provider_cooldown(provider) is not None:
                break  # _assess_risk falls back without a provider call.
            if self._acquire_provider_slot(provider, blocking=False):
                slot_held = acquired = True
                break
            if is_superseded():
                raise ProviderSuperseded("review superseded while waiting for risk provider capacity")
            time.sleep(_RISK_CAPACITY_POLL_SECONDS)
        try:
            with self._risk_cache_guard:
                cached = self._risk_cache.get(key)
                if cached is not None:
                    return cached
                lock = self._risk_cache_locks.get(key)
                if lock is None:
                    lock = threading.Lock()
                    self._risk_cache_locks[key] = lock
            with lock:
                with self._risk_cache_guard:
                    cached = self._risk_cache.get(key)
                    if cached is not None:
                        return cached
                try:
                    risk = self._assess_risk(job, source_commit, risk_config, is_superseded)
                except Exception:
                    with self._risk_cache_guard:
                        self._risk_cache_locks.pop(key, None)
                    raise
                with self._risk_cache_guard:
                    self._risk_cache[key] = risk
                    self._risk_cache_locks.pop(key, None)
                return risk
        finally:
            if acquired:
                self._release_provider_slot(provider)

    def _ensure_risk_cache(self) -> None:
        if not hasattr(self, "_risk_cache"):
            self._risk_cache = {}
        if not hasattr(self, "_risk_cache_locks"):
            self._risk_cache_locks = {}
        if not hasattr(self, "_risk_cache_guard"):
            self._risk_cache_guard = threading.Lock()

    def _assess_risk(self, job: ReviewJob, source_commit: str, risk_config, is_superseded: Callable[[], bool]) -> str:
        provider = getattr(risk_config, "provider", "codex")
        runner = self.providers.get(provider)
        if runner is None:
            LOG.warning("risk provider is not available provider=%s job=%s", provider, job.id)
            return DEFAULT_RISK
        cooldown_until = self.state.get_active_provider_cooldown(provider)
        if cooldown_until is not None:
            LOG.info("risk provider cooldown active provider=%s until=%s job=%s", provider, cooldown_until, job.id)
            return DEFAULT_RISK
        run_dir = str(Path(self.config.service.state_dir) / "runs" / str(job.id) / "risk")
        try:
            if provider == "codex":
                risk = runner.assess_risk(
                    description=job.description,
                    model=risk_config.model,
                    reasoning_effort=risk_config.effort,
                    timeout_seconds=risk_config.timeout_seconds,
                    run_dir=run_dir,
                    is_superseded=is_superseded,
                )
            elif provider == "claude":
                risk = runner.assess_risk(
                    description=job.description,
                    model=risk_config.model,
                    effort=risk_config.effort,
                    timeout_seconds=risk_config.timeout_seconds,
                    run_dir=run_dir,
                    is_superseded=is_superseded,
                )
            else:
                LOG.warning("unsupported risk provider provider=%s job=%s", provider, job.id)
                return DEFAULT_RISK
        except ProviderSuperseded:
            raise
        except ProviderError as exc:
            provider_cooldown_seconds = getattr(exc, "cooldown_seconds", None)
            if provider_cooldown_seconds:
                provider_status = exc.provider_status or PROVIDER_COOLDOWN_STATUS
                cooldown_until = self.state.mark_provider_cooldown(
                    provider,
                    str(exc),
                    provider_cooldown_seconds,
                    provider_status,
                )
                LOG.warning(
                    "provider cooldown set from risk classification provider=%s status=%s until=%s job=%s",
                    provider,
                    provider_status,
                    cooldown_until,
                    job.id,
                )
            LOG.warning("risk classification failed provider=%s job=%s error=%s", provider, job.id, exc)
            return DEFAULT_RISK
        except Exception as exc:
            LOG.warning("risk classification failed provider=%s job=%s error=%s", provider, job.id, exc)
            return DEFAULT_RISK
        normalized = normalize_risk(risk)
        LOG.info("risk classification job=%s provider=%s risk=%s", job.id, provider, normalized)
        return normalized

    def _lease_seconds(self, provider: str) -> int:
        provider_config = self.provider_configs.get(provider)
        if provider_config is None:
            # Frozen publication jobs can outlive their provider configuration.
            # Keep the lease grace even when the queue timeout is only one second;
            # renewal subtracts a second for SQLite timestamp truncation.
            return _lease_seconds(self.config.queue.job_timeout_seconds, 0)
        return _lease_seconds(
            self.config.queue.job_timeout_seconds,
            provider_config.timeout_seconds,
            self._risk_timeout_seconds(),
        )

    def _risk_timeout_seconds(self) -> int:
        risk_config = getattr(getattr(self.config, "review", None), "risk", None)
        if risk_config is None or not getattr(risk_config, "enabled", True):
            return 0
        return int(getattr(risk_config, "timeout_seconds", 0) or 0)

    def _ensure_provider_slots(self) -> None:
        if not hasattr(self, "_provider_slots"):
            self._provider_slots = {}
        if not hasattr(self, "_provider_slots_guard"):
            self._provider_slots_guard = threading.Lock()
        with self._provider_slots_guard:
            for provider, provider_config in getattr(self, "provider_configs", {}).items():
                if provider in self._provider_slots:
                    continue
                max_parallel = int(getattr(provider_config, "max_parallel", 1) or 1)
                if max_parallel < 1:
                    max_parallel = 1
                self._provider_slots[provider] = threading.BoundedSemaphore(max_parallel)

    def _acquire_provider_slot(self, provider: str, blocking: bool) -> bool:
        self._ensure_provider_slots()
        slot = self._provider_slots.get(provider)
        if slot is None:
            return True
        return slot.acquire(blocking=blocking)

    def _release_provider_slot(self, provider: str) -> None:
        slot = getattr(self, "_provider_slots", {}).get(provider)
        if slot is not None:
            slot.release()

    def cleanup_old_artifacts(self) -> None:
        try:
            cleanup_review_artifacts(
                self.config.service.state_dir,
                self.config.service.retention_days,
            )
        except Exception as exc:
            LOG.warning("review artifact cleanup failed error=%s", exc)


def _provider_config(config: AppConfig, provider: str):
    if provider == "codex":
        return config.agents.codex
    if provider == "claude":
        return config.agents.claude
    raise ConfigError("unsupported provider: {}".format(provider))


def _provider_runner(config: AppConfig, credentials: CredentialStore, provider: str):
    if provider == "codex":
        return CodexRunner(config.agents.codex, credentials)
    if provider == "claude":
        return ClaudeRunner(config.agents.claude, credentials)
    raise ConfigError("unsupported provider: {}".format(provider))


def _bitbucket_credentials(config: AppConfig, credentials: CredentialStore) -> BitbucketCredentials:
    if config.bitbucket.api_auth == "basic":
        return BitbucketCredentials(
            username=credentials.read(config.bitbucket.api_username_credential),
            api_key=credentials.read(config.bitbucket.api_key_credential),
        )
    if config.bitbucket.api_auth == "oauth_client_credentials":
        return BitbucketCredentials(
            username="",
            api_key="",
            auth_type="oauth_client_credentials",
            oauth_client_id=credentials.read(config.bitbucket.oauth_client_id_credential),
            oauth_client_secret=credentials.read(config.bitbucket.oauth_client_secret_credential),
            oauth_token_url=config.bitbucket.oauth_token_url,
        )
    raise ConfigError("unsupported bitbucket.api_auth {}".format(config.bitbucket.api_auth))


def _provider_model_metadata(provider: str, provider_config) -> str:
    model = getattr(provider_config, "model", "")
    if provider == "codex":
        effort = getattr(provider_config, "reasoning_effort", "")
    elif provider == "claude":
        effort = getattr(provider_config, "effort", "")
    else:
        raise ConfigError("unsupported provider: {}".format(provider))
    return _format_provider_model_metadata(model, effort)


def _format_provider_model_metadata(model: str, effort: str) -> str:
    model = str(model).strip()
    effort = str(effort).strip()
    model_label = model or "CLI default"
    effort_label = effort or "CLI default"
    return "{} / {}".format(model_label, effort_label)


def _selected_provider_config(config: AppConfig):
    return _provider_config(config, config.agents.strategy)


def _selected_provider_runner(config: AppConfig, credentials: CredentialStore):
    return _provider_runner(config, credentials, config.agents.strategy)


def _lease_renewal_interval(lease_seconds: int) -> float:
    return min(30.0, lease_seconds / 3)


def _lease_seconds(
    queue_timeout_seconds: int,
    provider_timeout_seconds: int,
    risk_timeout_seconds: int = 0,
) -> int:
    return max(queue_timeout_seconds, provider_timeout_seconds + risk_timeout_seconds + 60)


def _retry_backoff_seconds(config: AppConfig) -> int:
    return getattr(config.queue, "retry_backoff_seconds", 300)


def _risk_cache_key(job: ReviewJob, source_commit: str) -> tuple:
    return (
        job.workspace,
        job.repo_slug,
        job.pr_id,
        source_commit,
        job.destination_branch,
        job.destination_commit_hash or "",
        job.merge_base_hash or "",
        job.reviewer_policy_version,
        job.schema_version,
        job.description or "",
    )


def _seconds_until_next_poll(next_poll_at: float, now: float) -> float:
    return max(0.0, next_poll_at - now)


def _comment_severities(config: AppConfig):
    comments = getattr(config, "comments", None)
    if comments is None:
        return ["CRITICAL"]
    if hasattr(comments, "severities"):
        return list(getattr(comments, "severities"))
    if bool(getattr(comments, "critical_enabled", True)):
        return ["CRITICAL"]
    return []


def _comment_raw_content(comment: Dict[str, object]) -> str:
    content = comment.get("content")
    if not isinstance(content, dict):
        return ""
    raw = content.get("raw")
    return raw if isinstance(raw, str) else ""


def _pull_request_comment_exists(bitbucket, repo_slug: str, pr_id: int, content: str, before_request=None) -> bool:
    trusted_author = _trusted_bitbucket_comment_author(bitbucket)
    for comment in bitbucket.list_pull_request_comments(repo_slug, pr_id, before_request=before_request):
        if comment.get("deleted") is True:
            continue
        if not _comment_author_matches(comment, trusted_author):
            continue
        if _comment_raw_content(comment).strip() == content.strip():
            return True
    return False


def _trusted_bitbucket_comment_author(bitbucket) -> Optional[str]:
    credentials = getattr(bitbucket, "credentials", None)
    username = getattr(credentials, "username", None)
    return str(username) if username else None


def _comment_author_matches(comment: Dict[str, object], trusted_author: Optional[str]) -> bool:
    if not trusted_author:
        return False
    user = comment.get("user")
    if not isinstance(user, dict):
        return False
    trusted = _normalize_bitbucket_user_id(trusted_author)
    for field in ("account_id", "nickname", "username", "uuid"):
        value = user.get(field)
        if isinstance(value, str) and _normalize_bitbucket_user_id(value) == trusted:
            return True
    return False


def _normalize_bitbucket_user_id(value: str) -> str:
    return value.strip().strip("{}").lower()


def _safe_path_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip(".-")
    return segment[:80] or "unknown"


def _reap_worker_futures(done, futures: dict) -> None:
    for future in done:
        job_id = _future_job_id(futures.pop(future, None))
        try:
            future.result()
        except Exception:
            LOG.exception("review worker failed unexpectedly job=%s", job_id)


def _future_job_id(metadata) -> object:
    if isinstance(metadata, dict):
        return metadata.get("id")
    return metadata


def _future_provider(metadata) -> str:
    if isinstance(metadata, dict):
        return metadata.get("provider", "")
    return ""


def _review_log_entry(
    job: ReviewJob,
    source_commit: str,
    review: ValidatedReview,
    run_dir: str,
    usage: Optional[Dict[str, object]] = None,
    related_repositories: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, object]:
    summary = summarize_findings(review)
    entry = {
        "timestamp": utcnow(),
        "provider": job.provider,
        "workspace": job.workspace,
        "repo": job.repo_slug,
        "pr": job.pr_id,
        "commit": source_commit,
        "recommendation": review.recommendation,
        "findings_count": summary["total"],
        "findings_summary": {
            "by_reviewer": summary["by_reviewer"],
            "by_severity": summary["by_severity"],
            "by_reviewer_and_severity": summary["by_reviewer_and_severity"],
        },
        "raw_provider_logs": _raw_provider_log_paths(job.provider, run_dir),
        "related_repositories": list(related_repositories or []),
    }
    if usage is not None:
        entry["usage"] = usage
    return entry


def _append_review_log_entry(state_dir: str, entry: Dict[str, object]) -> Path:
    path = Path(state_dir) / "review-log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _REVIEW_LOG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    return path


def _provider_usage_log_entry(
    job: ReviewJob,
    source_commit: str,
    run_dir: str,
    status: str,
    usage: Optional[Dict[str, object]] = None,
    error: Optional[str] = None,
    related_repositories: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, object]:
    entry = {
        "timestamp": utcnow(),
        "provider": job.provider,
        "workspace": job.workspace,
        "repo": job.repo_slug,
        "pr": job.pr_id,
        "commit": source_commit,
        "job_id": job.id,
        "attempt": job.attempts,
        "status": status,
        "raw_provider_logs": _raw_provider_log_paths(job.provider, run_dir),
    }
    if usage is not None:
        entry["usage"] = usage
    if error:
        entry["error"] = error[:1000]
    if related_repositories:
        entry["related_repositories"] = list(related_repositories)
    return entry


def _provider_usage_log_entry_from_logs(
    job: ReviewJob,
    source_commit: str,
    run_dir: str,
    status: str,
    error: Optional[str] = None,
    related_repositories: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, object]:
    return _provider_usage_log_entry(
        job,
        source_commit,
        run_dir,
        status,
        parse_provider_usage_from_logs(job.provider, run_dir),
        error,
        related_repositories,
    )


def _append_provider_usage_log_entry(state_dir: str, entry: Dict[str, object]) -> Path:
    path = Path(state_dir) / "provider-usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _REVIEW_LOG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    return path


def _raw_provider_log_paths(provider: str, run_dir: str) -> Dict[str, str]:
    run_path = Path(run_dir)
    paths = {
        "stdout": str(run_path / "{}-stdout.log".format(provider)),
        "stderr": str(run_path / "{}-stderr.log".format(provider)),
    }
    if provider == "codex":
        paths["final_message"] = str(run_path / "codex-final-message.json")
    return paths
