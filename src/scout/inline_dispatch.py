"""Dispatch round selection and publication on the daemon's shared worker pool."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .deduplication import exact_selection, extract_selection, prepare_selection
from .inline_publisher import InlinePublisher, make_candidates
from .provider import PROVIDER_COOLDOWN_STATUS, ProviderError, ProviderSuperseded

LOG = logging.getLogger(__name__)


def _expired(value):
    return value is not None and datetime.fromisoformat(value) <= datetime.now(timezone.utc)


class InlineDispatch:
    def __init__(self, daemon):
        self.daemon = daemon
        self.state = daemon.state.inline
        self.config = daemon.config
        self.publisher = InlinePublisher(daemon.state, daemon.bitbucket, daemon.config)
        self.locks = {}

    def close_missing_prs(self, workspace, repo_slug, open_pr_ids):
        keep = set(open_pr_ids)
        rounds = self.state.list_rounds(workspace=workspace, repo_slug=repo_slug)
        known = {item["pr_id"] for item in rounds}
        for pr_id in known - keep:
            # Known closure fences further sends immediately, even while a
            # publisher is using cached PR metadata or an HTTP call is in flight.
            for round_ in rounds:
                if round_["pr_id"] == pr_id:
                    self.state.cancel_round(round_["id"], "Pull request closed")
            key = (workspace, repo_slug, pr_id)
            active = any(not future.done() and metadata.get("pr") == key
                         for future, metadata in getattr(self.daemon, "_worker_futures", {}).items())
            if active:
                keep.add(pr_id)
                continue
            lock = self.locks.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=False):
                keep.add(pr_id)
                continue
            try:
                self.publisher.close_pr(workspace, repo_slug, pr_id)
            finally:
                lock.release()
        return sorted(keep)

    def maintain(self):
        for round_ in self.state.list_rounds(statuses=["reviewing"]):
            # A disabled member cannot rescue a cooled-down provider, even when
            # it appears later in the round's frozen provider order.
            for outcome in round_["outcomes"]:
                if outcome["status"] == "pending" and outcome["provider"] not in self.daemon.provider_names:
                    self.state.fail_provider(round_["id"], outcome["provider"], "provider disabled")
            for outcome in round_["outcomes"]:
                provider = outcome["provider"]
                if outcome["status"] != "pending" or provider not in self.daemon.provider_names:
                    continue
                if self.daemon.state.get_active_provider_cooldown(provider) is not None:
                    self.state.fail_or_recover_for_cooldown(
                        round_["id"], provider, "provider cooldown", self.config.queue.max_provider_recovery_seconds,
                    )
        self.state.expire_provider_recovery()

    def schedule(self, pool, futures):
        active = {item.get("round_id") for item in futures.values() if isinstance(item, dict)}
        for round_ in self.state.list_rounds(statuses=["ready_for_selection", "selecting", "publishing"]):
            if round_["id"] in active:
                continue
            key = (round_["workspace"], round_["repo_slug"], round_["pr_id"])
            lock = self.locks.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=False):
                continue
            reserved = None
            dispatched = False
            try:
                provider = self._selection_provider(round_)
                if provider:
                    if self.daemon.state.get_active_provider_cooldown(provider) is not None:
                        self.state.start_selection_recovery(round_["id"], self.config.queue.max_selection_recovery_seconds)
                        continue
                    if not self.daemon._acquire_provider_slot(provider, blocking=False):
                        continue
                    reserved = provider
                lease_seconds = max(self.config.queue.job_timeout_seconds, self.config.review.deduplication.timeout_seconds + 60)
                claimed = self.state.claim_round(round_["id"], lease_seconds)
                if claimed is None:
                    continue
                future = pool.submit(self.run, claimed, lock, reserved, lease_seconds)
                futures[future] = {"round_id": round_["id"], "provider": reserved or "", "id": None, "pr": key}
                dispatched = True
                return True
            finally:
                if not dispatched:
                    if reserved:
                        self.daemon._release_provider_slot(reserved)
                    lock.release()
        return False

    def _selection_provider(self, round_):
        config = self.config.review.deduplication
        if self.state.get_plan(round_["id"]) is not None or not config.enabled or not self.publisher.identity:
            return None
        if _expired(round_.get("selection_recovery_deadline_at")):
            return None
        if round_.get("selection_attempts", 0) >= self.config.queue.publication_max_attempts:
            return None
        if not make_candidates(round_):
            return None
        return config.provider

    def run(self, round_, lock, reserved, lease_seconds):
        token = round_["lease_token"]
        round_id = round_["id"]
        stop = threading.Event()
        lost = threading.Event()

        def check():
            if lost.is_set() or not self.state.renew_round(round_id, token, lease_seconds):
                lost.set()
                raise ProviderSuperseded("inline publication lease lost")

        def heartbeat():
            while not stop.wait(min(30, lease_seconds / 3)):
                try:
                    check()
                except Exception:
                    lost.set()
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, name="inline-lease-{}".format(round_id), daemon=True)
        heartbeat_thread.start()
        try:
            check()
            plan = self.state.get_plan(round_id)
            if plan is None:
                if not self.publisher.reconcile(round_, before_request=check, current_snapshot=lambda: self._snapshot(round_)):
                    self.state.release_round(round_id, token, backoff_seconds=self.config.queue.publication_settle_seconds)
                    return
                candidates = make_candidates(round_)
                history = self.publisher.history(round_, before_request=check)
                enabled = self.config.review.deduplication.enabled and bool(self.publisher.identity)
                mode = "disabled" if not enabled else "fallback"
                if reserved and enabled:
                    try:
                        selection = self._select(round_, candidates, history, lost)
                        mode = "model"
                    except ProviderSuperseded:
                        raise
                    except Exception as exc:
                        self._selection_failed(round_, token, exc)
                        return
                    finally:
                        self.daemon._release_provider_slot(reserved)
                        reserved = None
                else:
                    # A selector that cannot recover publishes every candidate rather than
                    # guessing at duplicates from exact matches alone.
                    selection = exact_selection(candidates, history, enabled=False)
                check()
                if not self.state.save_plan(round_id, token, selection.to_dict(), kind=mode):
                    self.state.release_round(round_id, token)
                    return
            if reserved:
                self.daemon._release_provider_slot(reserved)
                reserved = None
            # Saved plans reconcile in the publisher, which also marks exhausted
            # uncertain deliveries as publication_failed for operator recovery.
            status = self.publisher.publish_round(round_id, token, lambda: self._snapshot(round_), before_request=check)
            self.state.release_round(round_id, token, status=status,
                                     backoff_seconds=self.config.queue.publication_retry_backoff_seconds if status == "publishing" else 0)
        except ProviderSuperseded:
            pass
        except Exception as exc:
            LOG.exception("inline publication failed round=%s", round_id)
            self.state.publication_failed(round_id, token, str(exc), self.config.queue.publication_max_attempts,
                                          self.config.queue.publication_retry_backoff_seconds)
        finally:
            if reserved:
                self.daemon._release_provider_slot(reserved)
            stop.set()
            heartbeat_thread.join()
            lock.release()

    def _select(self, round_, candidates, history, lost):
        config = self.config.review.deduplication
        selection_input = prepare_selection(candidates, history, config.max_input_findings, config.max_input_bytes)
        if not selection_input.candidates:
            return extract_selection('{"decisions":[],"historical_supersessions":[]}', selection_input)
        timeout = config.timeout_seconds
        deadline = round_.get("selection_recovery_deadline_at")
        if deadline:
            timeout = min(timeout, max(1, int((datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds())))
        response = self.daemon.providers[config.provider].classify_findings(
            prompt=selection_input.prompt, schema_json=selection_input.schema_json,
            model=config.model, effort=config.effort, timeout_seconds=timeout,
            run_dir=str(Path(self.config.service.state_dir) / "runs" / "selection" / str(round_["id"])),
            is_superseded=lost.is_set,
        )
        return extract_selection(response, selection_input)

    def _selection_failed(self, round_, token, error):
        provider = self.config.review.deduplication.provider
        if isinstance(error, ProviderError) and error.cooldown_seconds:
            self.daemon.state.mark_provider_cooldown(provider, str(error), error.cooldown_seconds,
                                                     error.provider_status or PROVIDER_COOLDOWN_STATUS)
        self.state.start_selection_recovery(round_["id"], self.config.queue.max_selection_recovery_seconds)
        self.state.selection_failed(round_["id"], token, str(error),
                                    permanent=not getattr(error, "retryable", True),
                                    backoff_seconds=self.config.queue.publication_retry_backoff_seconds)

    def _snapshot(self, round_):
        pr = self.daemon.bitbucket.get_pull_request(round_["repo_slug"], round_["pr_id"])
        if not self.daemon._inline_pr_eligible(pr):
            self.state.cancel_round(round_["id"], "PR is no longer eligible")
            raise ProviderSuperseded("PR is no longer eligible")
        return pr
