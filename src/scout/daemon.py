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
from .config import AppConfig, ConfigError, CredentialStore
from .gitops import GitError, GitManager
from .models import PullRequest
from .prompt import build_provider_prompt
from .provider import PROVIDER_COOLDOWN_STATUS, ProviderError, ProviderSuperseded
from .retention import cleanup_review_artifacts
from .review_plan import build_review_plan
from .runtime_lock import RuntimeLock
from .schema import (
    ReviewValidationError,
    ValidatedReview,
    parse_review_json,
    summarize_findings,
    to_bitbucket_annotations,
    to_pr_comment,
    to_bitbucket_report,
    validate_review_output,
)
from .state import ReviewJob, StateStore, utcnow
from .usage import parse_provider_usage_from_logs

LOG = logging.getLogger(__name__)
_REVIEW_LOG_LOCK = threading.Lock()


class ScoutDaemon:
    def __init__(self, config: AppConfig, credentials: CredentialStore):
        self.config = config
        self.credentials = credentials
        self.state = StateStore(config.service.state_db)
        self.bitbucket = BitbucketClient(
            base_url=config.bitbucket.api_base_url,
            workspace=config.bitbucket.workspace,
            credentials=BitbucketCredentials(
                username=credentials.read(config.bitbucket.api_username_credential),
                api_key=credentials.read(config.bitbucket.api_key_credential),
            ),
        )
        try:
            ssh_key_path = str(credentials.path(config.bitbucket.ssh_key_credential))
        except ConfigError:
            ssh_key_path = None
        self.git = GitManager(config.service.state_dir, ssh_key_path=ssh_key_path)
        self.provider_names = list(config.agents.providers)
        self.provider_configs = {
            provider: _provider_config(config, provider)
            for provider in self.provider_names
        }
        self.providers = {
            provider: _provider_runner(config, credentials, provider)
            for provider in self.provider_names
        }
        self.max_parallel_reviews = min(
            config.queue.max_parallel_reviews,
            sum(provider.max_parallel for provider in self.provider_configs.values()),
        )
        self.clone_urls: Dict[str, str] = {
            repo.slug: repo.clone_url for repo in config.bitbucket.repositories
        }

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
                prs = self.bitbucket.list_open_pull_requests(repo.slug, self.config.polling.pagelen)
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
            if getattr(repo, "ignore_draft_pull_requests", False):
                ignored_draft_pr_ids = [pr.pr_id for pr in prs if pr.is_draft]
                if ignored_draft_pr_ids:
                    ignored_set = set(ignored_draft_pr_ids)
                    ignored_count = self.state.prune_ignored_pull_requests(
                        self.config.bitbucket.workspace,
                        repo.slug,
                        ignored_draft_pr_ids,
                        "PR is draft and repository is configured to ignore draft pull requests",
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
                for provider in self.provider_names:
                    policy_version = self.config.review.policy_version
                    schema_version = "v1"
                    if (
                        not self.state.has_review_for_key(pr, policy_version, schema_version, provider)
                        and self.state.should_bootstrap_report(pr, policy_version, schema_version, provider)
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
                    )
                    if queued:
                        LOG.info(
                            "queued or updated review provider=%s repo=%s pr=%s commit=%s",
                            provider,
                            pr.repo_slug,
                            pr.pr_id,
                            pr.source_commit_hash,
                        )

    def _is_ignored_source_branch(self, repo, source_branch: str) -> bool:
        for pattern in getattr(repo, "ignored_source_branches", ()):
            if re.search(pattern, source_branch):
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
            review_plan = build_review_plan(
                changed_lines=int(context["changed_lines"]),
                description=pr.description,
                small_loc_limit=provider_config.subagent_small_loc_limit,
                medium_loc_limit=provider_config.subagent_medium_loc_limit,
                large_loc_limit=provider_config.subagent_large_loc_limit,
                high_risk_bonus=provider_config.subagent_high_risk_bonus,
                max_subagents_per_lens=provider_config.subagent_max_per_lens,
            )
            LOG.info(
                "review plan job=%s changed_lines=%s high_risk=%s subagents_per_lens=%s total_subagents=%s",
                job.id,
                review_plan.changed_lines,
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
            result = provider_runner.run(
                worktree=str(worktree),
                prompt=prompt,
                schema_path=self.config.review.schema_path,
                run_dir=run_dir,
                is_superseded=lambda: self.state.is_job_superseded(job.id, job.lease_token),
            )
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
            report_id = self.config.reports.report_id_for(job.provider)
            report_title = self.config.reports.title_for(job.provider)
            report = to_bitbucket_report(validated, report_title, provider=job.provider)
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

    def _lease_seconds(self, provider: str) -> int:
        return _lease_seconds(
            self.config.queue.job_timeout_seconds,
            self.provider_configs[provider].timeout_seconds,
        )

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


def _selected_provider_config(config: AppConfig):
    return _provider_config(config, config.agents.strategy)


def _selected_provider_runner(config: AppConfig, credentials: CredentialStore):
    return _provider_runner(config, credentials, config.agents.strategy)


def _lease_seconds(queue_timeout_seconds: int, provider_timeout_seconds: int) -> int:
    return max(queue_timeout_seconds, provider_timeout_seconds + 60)


def _retry_backoff_seconds(config: AppConfig) -> int:
    return getattr(config.queue, "retry_backoff_seconds", 300)


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
