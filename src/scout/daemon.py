from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Dict, Optional

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
from .review_plan import DEFAULT_RISK, build_review_plan, normalize_risk
from .runtime_lock import RuntimeLock
from .schema import (
    ReviewValidationError,
    ValidatedReview,
    parse_review_json,
    summarize_findings,
    to_bitbucket_annotations,
    to_pr_comment,
    to_bitbucket_report,
    to_inline_pr_comments,
    to_no_findings_pr_comment,
    validate_review_output,
)
from .state import ReviewJob, StateStore, utcnow
from .usage import parse_provider_usage_from_logs

LOG = logging.getLogger(__name__)
_REVIEW_LOG_LOCK = threading.Lock()
_NO_FINDINGS_INLINE_COMMENT_ID = "__scout_no_findings__"


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
        self.provider_configs = {
            provider: _provider_config(config, provider)
            for provider in runtime_provider_names
        }
        self.providers = {
            provider: _provider_runner(config, credentials, provider)
            for provider in runtime_provider_names
        }
        self.max_parallel_reviews = min(
            config.queue.max_parallel_reviews,
            sum(self.provider_configs[provider].max_parallel for provider in self.provider_names),
        )
        self.clone_urls: Dict[str, str] = {
            repo.slug: repo.clone_url for repo in config.bitbucket.repositories
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
            self.state.upsert_repository(self.config.bitbucket.workspace, repo.slug, repo.clone_url)
        self.validate_startup()
        recovered = self.state.recover_abandoned_jobs()
        if recovered:
            LOG.info("recovered abandoned active review jobs count=%s", recovered)
        self.cleanup_old_artifacts()

    def validate_startup(self) -> None:
        for repo in self.config.bitbucket.repositories:
            self.bitbucket.validate_repository(repo.slug)
            self.git.validate_clone_url(repo.clone_url)
        for provider in self.providers.values():
            provider.validate_startup()

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
                    wait_timeout = _seconds_until_next_poll(next_poll_at, time.monotonic())
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

    def poll_once(self) -> None:
        if not self.config.polling.enabled:
            return
        for repo in self.config.bitbucket.repositories:
            LOG.info("polling repository workspace=%s repo=%s", self.config.bitbucket.workspace, repo.slug)
            try:
                prs = self.bitbucket.list_open_pull_requests(repo.slug)
            except BitbucketError as exc:
                LOG.error("Bitbucket poll failed repo=%s retryable=%s error=%s", repo.slug, exc.retryable, exc)
                continue
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
                pruned = self.state.prune_closed_pull_requests(
                    self.config.bitbucket.workspace,
                    repo.slug,
                    [pr.pr_id for pr in prs],
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

            for provider in self.provider_names:
                queued = self.state.force_enqueue_pr_review(
                    pr,
                    policy_version,
                    schema_version,
                    provider,
                    output_mode=output_mode,
                )
                if queued:
                    LOG.info(
                        "queued review from Scout mention provider=%s repo=%s pr=%s comment=%s",
                        provider,
                        pr.repo_slug,
                        pr.pr_id,
                        comment_id,
                    )
            self.state.mark_pull_request_comment_processed(
                pr.workspace,
                pr.repo_slug,
                pr.pr_id,
                comment_id,
                updated_on,
                True,
            )

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

    def run_job(self, job: ReviewJob) -> None:
        LOG.info("starting review job id=%s repo=%s pr=%s commit=%s", job.id, job.repo_slug, job.pr_id, job.running_source_commit_hash)
        provider_config = self.provider_configs[job.provider]
        provider_runner = self.providers[job.provider]
        cooldown_until = self.state.get_active_provider_cooldown(job.provider)
        if cooldown_until is not None:
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
        mirror = None
        worktree = None
        run_dir = None
        source_commit = job.running_source_commit_hash or job.target_source_commit_hash
        usage_logged = False
        try:
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
            worktree = self.git.create_worktree(mirror, pr, suffix="job-{}".format(job.id))
            context = self.git.prepare_context(mirror, worktree, pr)
            risk = self._risk_for_job(job, source_commit)
            review_plan = build_review_plan(
                changed_lines=int(context["changed_lines"]),
                description=pr.description,
                small_loc_limit=provider_config.subagent_small_loc_limit,
                medium_loc_limit=provider_config.subagent_medium_loc_limit,
                large_loc_limit=provider_config.subagent_large_loc_limit,
                high_risk_bonus=provider_config.subagent_high_risk_bonus,
                max_subagents_per_lens=provider_config.subagent_max_per_lens,
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
            self._acquire_provider_slot(job.provider, blocking=True)
            try:
                result = provider_runner.run(
                    worktree=str(worktree),
                    prompt=prompt,
                    schema_path=self.config.review.schema_path,
                    run_dir=run_dir,
                    is_superseded=lambda: self.state.is_job_superseded(job.id, job.lease_token),
                )
            finally:
                self._release_provider_slot(job.provider)
            _append_provider_usage_log_entry(
                self.config.service.state_dir,
                _provider_usage_log_entry(job, source_commit, run_dir, "provider_completed", result.usage),
            )
            usage_logged = True
            if self.state.is_job_superseded(job.id, job.lease_token):
                raise ProviderSuperseded("review superseded before publish")
            parsed = parse_review_json(result.final_message)
            validated = validate_review_output(parsed, max_findings=self.config.review.max_findings)
            if not self.state.mark_publishing(job, self._lease_seconds(job.provider)):
                raise ProviderSuperseded("review superseded before publish")
            review_log_path = _append_review_log_entry(
                self.config.service.state_dir,
                _review_log_entry(job, source_commit, validated, run_dir, result.usage),
            )
            LOG.info(
                "appended review log path=%s job=%s repo=%s pr=%s",
                review_log_path,
                job.id,
                job.repo_slug,
                job.pr_id,
            )
            output_mode = getattr(job, "output_mode", getattr(self.config.review, "output_mode", "reports"))
            if output_mode == "inline_comments":
                report_id = "inline-comments"
                review_run_id = job.running_review_run_id or job.target_review_run_id
                no_findings_comment = to_no_findings_pr_comment(validated, provider=job.provider)
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
                for comment in to_inline_pr_comments(
                    validated,
                    provider=job.provider,
                    source_commit=source_commit,
                    review_run_id=review_run_id,
                ):
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
                        before_request=lambda: self._renew_publish_or_superseded(job),
                    )
                    self.state.mark_inline_comment_published(job, external_id)
            else:
                report_id = self.config.reports.report_id_for(job.provider)
                report_title = self.config.reports.title_for(job.provider)
                report = to_bitbucket_report(
                    validated,
                    report_title,
                    provider=job.provider,
                    model_metadata=_provider_model_metadata(job.provider, provider_config),
                )
                annotations = to_bitbucket_annotations(validated, provider=job.provider)
                if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
                    raise ProviderSuperseded("review superseded before report publish")
                self.bitbucket.publish_report(job.repo_slug, source_commit, report_id, report)
                if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
                    raise ProviderSuperseded("review superseded before annotation publish")
                self.bitbucket.publish_annotations(
                    job.repo_slug,
                    source_commit,
                    report_id,
                    annotations,
                    before_request=lambda: self._renew_publish_or_superseded(job),
                )
                pr_comment = to_pr_comment(
                    validated,
                    provider=job.provider,
                    source_commit=source_commit,
                    severities=_comment_severities(config=self.config),
                )
                if pr_comment:
                    if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
                        raise ProviderSuperseded("review superseded before PR comment publish")
                    self.bitbucket.publish_pull_request_comment(
                        job.repo_slug,
                        job.pr_id,
                        pr_comment,
                        before_request=lambda: self._renew_publish_or_superseded(job),
                    )
            if not self.state.mark_success(job, report_id):
                raise ProviderSuperseded("review superseded before success mark")
            LOG.info("review job succeeded id=%s repo=%s pr=%s", job.id, job.repo_slug, job.pr_id)
        except ProviderSuperseded as exc:
            LOG.info("review job superseded id=%s error=%s", job.id, exc)
            if run_dir is not None and not usage_logged:
                _append_provider_usage_log_entry(
                    self.config.service.state_dir,
                    _provider_usage_log_entry_from_logs(job, source_commit, run_dir, "superseded", str(exc)),
                )
            self.state.return_superseded_to_pending(job.id, job.lease_token)
        except (BitbucketError, GitError, ProviderError, ReviewValidationError) as exc:
            retryable = getattr(exc, "retryable", True)
            LOG.error("review job failed id=%s retryable=%s error=%s", job.id, retryable, exc)
            if run_dir is not None and not usage_logged:
                _append_provider_usage_log_entry(
                    self.config.service.state_dir,
                    _provider_usage_log_entry_from_logs(job, source_commit, run_dir, "failed", str(exc)),
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
            if isinstance(exc, ProviderError) and provider_cooldown_seconds:
                marked = self.state.defer_job_for_provider_cooldown(
                    job.id,
                    str(exc),
                    job.lease_token,
                    job.running_review_key,
                )
            elif retryable:
                marked = self.state.mark_retryable_failure(
                    job.id,
                    str(exc),
                    self.config.queue.max_attempts,
                    job.lease_token,
                    job.running_review_key,
                    _retry_backoff_seconds(self.config),
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
            if mirror is not None and worktree is not None:
                try:
                    self.git.remove_worktree(mirror, worktree)
                except Exception as exc:
                    LOG.warning("failed to remove worktree path=%s error=%s", worktree, exc)

    def _schedule(self, pool: ThreadPoolExecutor, futures: dict) -> None:
        capacity = self.max_parallel_reviews - len(futures)
        running_by_provider = Counter(_future_provider(metadata) for metadata in futures.values())
        while capacity > 0:
            lease_seconds_by_provider = {}
            for provider in self.provider_names:
                provider_capacity = self.provider_configs[provider].max_parallel - running_by_provider[provider]
                if provider_capacity > 0:
                    lease_seconds_by_provider[provider] = self._lease_seconds(provider)
            if not lease_seconds_by_provider:
                return
            job = self.state.claim_next_pending_job(lease_seconds_by_provider)
            if job is None:
                return
            futures[pool.submit(self.run_job, job)] = {
                "id": job.id,
                "provider": job.provider,
            }
            running_by_provider[job.provider] += 1
            capacity -= 1

    def _renew_publish_or_superseded(self, job: ReviewJob) -> None:
        if not self.state.renew_publishing_lease(job, self._lease_seconds(job.provider)):
            raise ProviderSuperseded("review superseded during publish")

    def _risk_for_job(self, job: ReviewJob, source_commit: str) -> str:
        risk_config = getattr(self.config.review, "risk", None)
        if risk_config is None or not getattr(risk_config, "enabled", True):
            return DEFAULT_RISK
        self._ensure_risk_cache()
        key = _risk_cache_key(job, source_commit)
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
                risk = self._assess_risk(job, source_commit, risk_config)
            except Exception:
                with self._risk_cache_guard:
                    self._risk_cache_locks.pop(key, None)
                raise
            with self._risk_cache_guard:
                self._risk_cache[key] = risk
                self._risk_cache_locks.pop(key, None)
            return risk

    def _ensure_risk_cache(self) -> None:
        if not hasattr(self, "_risk_cache"):
            self._risk_cache = {}
        if not hasattr(self, "_risk_cache_locks"):
            self._risk_cache_locks = {}
        if not hasattr(self, "_risk_cache_guard"):
            self._risk_cache_guard = threading.Lock()

    def _assess_risk(self, job: ReviewJob, source_commit: str, risk_config) -> str:
        provider = getattr(risk_config, "provider", "codex")
        runner = self.providers.get(provider)
        if runner is None:
            LOG.warning("risk provider is not available provider=%s job=%s", provider, job.id)
            return DEFAULT_RISK
        cooldown_until = self.state.get_active_provider_cooldown(provider)
        if cooldown_until is not None:
            LOG.info("risk provider cooldown active provider=%s until=%s job=%s", provider, cooldown_until, job.id)
            return DEFAULT_RISK
        if not self._acquire_provider_slot(provider, blocking=False):
            LOG.info("risk provider capacity unavailable provider=%s job=%s", provider, job.id)
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
                    is_superseded=lambda: self.state.is_job_superseded(job.id, job.lease_token),
                )
            elif provider == "claude":
                risk = runner.assess_risk(
                    description=job.description,
                    model=risk_config.model,
                    effort=risk_config.effort,
                    timeout_seconds=risk_config.timeout_seconds,
                    run_dir=run_dir,
                    is_superseded=lambda: self.state.is_job_superseded(job.id, job.lease_token),
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
        finally:
            self._release_provider_slot(provider)
        normalized = normalize_risk(risk)
        LOG.info("risk classification job=%s provider=%s risk=%s", job.id, provider, normalized)
        return normalized

    def _lease_seconds(self, provider: str) -> int:
        return _lease_seconds(
            self.config.queue.job_timeout_seconds,
            self.provider_configs[provider].timeout_seconds,
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
    return entry


def _provider_usage_log_entry_from_logs(
    job: ReviewJob,
    source_commit: str,
    run_dir: str,
    status: str,
    error: Optional[str] = None,
) -> Dict[str, object]:
    return _provider_usage_log_entry(
        job,
        source_commit,
        run_dir,
        status,
        parse_provider_usage_from_logs(job.provider, run_dir),
        error,
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
