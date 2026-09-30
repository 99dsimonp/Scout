"""Durable inline rounds, provider outcomes, and publication recovery.

All ownership checks and state transitions are short SQLite transactions; callers
perform model and HTTP operations after releasing the transaction.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .models import PullRequest, review_key


ACTIVE = ("reviewing", "ready_for_selection", "selecting", "publishing", "publication_failed")
PUBLISHABLE = ("ready_for_selection", "selecting", "publishing")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class InlineState:
    def __init__(self, store):
        self.store = store

    def initialize(self, conn) -> None:
        conn.executescript("""
            create table if not exists inline_rounds (
              id text primary key, workspace text not null, repo_slug text not null,
              pr_id integer not null, snapshot text not null, providers text not null,
              policy_version text not null, schema_version text not null, trigger text not null,
              status text not null, version integer not null default 0,
              selection_ready_at text, selection_recovery_deadline_at text,
              lease_token text, leased_until text, retry_after text,
              attempts integer not null default 0, selection_attempts integer not null default 0, error text,
              created_at text not null, updated_at text not null
            );
            create index if not exists inline_rounds_pr on inline_rounds(workspace,repo_slug,pr_id);
            create table if not exists inline_round_providers (
              round_id text not null, provider text not null, run_id text not null unique,
              status text not null default 'pending', result text, error text,
              recovery_deadline_at text, completed_at text,
              primary key(round_id,provider)
            );
            create table if not exists inline_selection_plans (
              round_id text primary key, kind text not null, plan text not null, created_at text not null
            );
            create table if not exists inline_publication_intents (
              id text primary key, round_id text not null, intent_key text not null,
              kind text not null, marker text not null, payload text not null,
              status text not null default 'ready', version integer not null default 0,
              attempts integer not null default 0, last_attempt_at text,
              comment_ids text not null default '[]', author_id text, error text,
              stale integer not null default 0, uncertain integer not null default 0, lease_token text,
              created_at text not null, updated_at text not null,
              unique(round_id,intent_key)
            );
            create table if not exists inline_candidate_outcomes (
              round_id text not null, candidate_id text not null, status text not null,
              target_id text, primary key(round_id,candidate_id)
            );
            create table if not exists inline_finding_history (
              workspace text not null, repo_slug text not null, pr_id integer not null,
              comment_id text not null, finding text not null, metadata text not null,
              primary key(workspace,repo_slug,pr_id,comment_id)
            );
            create table if not exists inline_bot_identities (
              identity text primary key, source text not null, verified_at text not null
            );
            create table if not exists inline_publication_audit (
              id integer primary key, intent_id text not null, previous_version integer not null,
              outcome text not null, comment_id text, created_at text not null
            );
        """)
        self.store._ensure_column(conn, "inline_rounds", "selection_attempts", "integer not null default 0")
        self.store._ensure_column(conn, "inline_publication_intents", "uncertain", "integer not null default 0")

    def _round(self, conn, row):
        if row is None:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("snapshot")))
        result["round_id"] = result["id"]
        result["providers"] = json.loads(result["providers"])
        outcomes = {}
        for outcome in conn.execute("select * from inline_round_providers where round_id=?", (result["id"],)):
            item = dict(outcome)
            item["result"] = json.loads(item["result"]) if item["result"] is not None else None
            outcomes[item["provider"]] = item
        result["outcomes"] = [outcomes[p] for p in result["providers"]]
        return result

    def get_round(self, round_id: str) -> Optional[dict]:
        with self.store.connect() as conn:
            return self._round(conn, conn.execute("select * from inline_rounds where id=?", (round_id,)).fetchone())

    def list_rounds(self, statuses=None, workspace=None, repo_slug=None, pr_id=None) -> list:
        clauses, params = [], []
        for key, value in (("workspace", workspace), ("repo_slug", repo_slug), ("pr_id", pr_id)):
            if value is not None:
                clauses.append(key + "=?")
                params.append(value)
        if statuses is not None:
            if not statuses:
                return []
            clauses.append("status in ({})".format(",".join("?" for _ in statuses)))
            params.extend(statuses)
        sql = "select * from inline_rounds"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by created_at,id"
        with self.store.connect() as conn:
            return [self._round(conn, row) for row in conn.execute(sql, params).fetchall()]

    def round_for_job(self, job) -> Optional[dict]:
        run_id = job.running_review_run_id or job.target_review_run_id
        with self.store.connect() as conn:
            row = conn.execute("""select r.* from inline_rounds r join inline_round_providers p
                on p.round_id=r.id where p.run_id=?""", (run_id,)).fetchone()
            return self._round(conn, row)

    def create_round(self, pr: PullRequest, providers: Sequence[str], policy_version: str,
                     schema_version: str, trigger: str = "initial", request_comment=None,
                     replace_round_id=None, expected_version=None) -> Optional[dict]:
        providers = tuple(dict.fromkeys(providers))
        if not providers:
            raise ValueError("An inline round requires at least one provider")
        now, round_id = utcnow(), uuid.uuid4().hex
        scope = (pr.workspace, pr.repo_slug, pr.pr_id)
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            if conn.execute("select 1 from repositories where workspace=? and repo_slug=? and enabled=0", scope[:2]).fetchone():
                return None
            previous = conn.execute("select * from inline_rounds where workspace=? and repo_slug=? and pr_id=? order by created_at desc", scope).fetchall()
            if trigger == "initial":
                # The pre-round publisher owns these frozen payloads and its existing
                # publication ledger. Replacing their run IDs would delete snapshots.
                if conn.execute("""select 1 from review_publication_snapshots s join review_jobs j on j.id=s.job_id
                    where j.workspace=? and j.repo_slug=? and j.pr_id=? and j.output_mode='inline_comments'
                    and s.review_run_id=j.target_review_run_id and j.status!='cancelled'""", scope).fetchone():
                    return None
                if previous or conn.execute("""select 1 from review_jobs where workspace=? and repo_slug=?
                    and pr_id=? and output_mode='inline_comments' and status='succeeded'""", scope).fetchone():
                    return None
            if request_comment is not None:
                if conn.execute("""select 1 from processed_pr_comments where workspace=? and repo_slug=?
                    and pr_id=? and comment_id=? and updated_on=?""", (*scope, str(request_comment[0]), request_comment[1])).fetchone():
                    return None
            if replace_round_id is not None:
                old = next((r for r in previous if r["id"] == replace_round_id), None)
                if old is None or old["status"] != "reviewing" or old["version"] != expected_version:
                    return None
                providers = tuple(json.loads(old["providers"]))
                policy_version, schema_version = old["policy_version"], old["schema_version"]
            for old in previous:
                if old["status"] in ACTIVE:
                    self._cancel(conn, old["id"], "Superseded by a new review round", "superseded", now)
            conn.execute("""insert into inline_rounds
                (id,workspace,repo_slug,pr_id,snapshot,providers,policy_version,schema_version,trigger,status,created_at,updated_at)
                values(?,?,?,?,?,?,?,?,?,'reviewing',?,?)""",
                (round_id, *scope, _json(asdict(pr)), _json(providers), policy_version, schema_version, trigger, now, now))
            conn.execute("""insert into pull_request_state(workspace,repo_slug,pr_id,title,description,source_branch,
                destination_branch,source_commit_hash,destination_commit_hash,merge_base_hash,review_status,updated_at)
                values(?,?,?,?,?,?,?,?,?,?,'pending',?) on conflict(workspace,repo_slug,pr_id) do update set
                title=excluded.title,description=excluded.description,source_branch=excluded.source_branch,
                destination_branch=excluded.destination_branch,source_commit_hash=excluded.source_commit_hash,
                destination_commit_hash=excluded.destination_commit_hash,merge_base_hash=excluded.merge_base_hash,
                review_status='pending',updated_at=excluded.updated_at""",
                (*scope, pr.title, pr.description, pr.source_branch, pr.destination_branch, pr.source_commit_hash,
                 pr.destination_commit_hash, pr.merge_base_hash, now))
            for provider in providers:
                run_id = uuid.uuid4().hex
                conn.execute("insert into inline_round_providers(round_id,provider,run_id) values(?,?,?)", (round_id, provider, run_id))
                conn.execute("""insert into review_jobs(workspace,repo_slug,pr_id,title,description,source_branch,
                    target_source_commit_hash,destination_branch,destination_commit_hash,merge_base_hash,
                    reviewer_policy_version,schema_version,provider,output_mode,status,target_review_key,
                    target_review_run_id,created_at,updated_at)
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,'inline_comments','pending',?,?,?,?)
                    on conflict(workspace,repo_slug,pr_id,reviewer_policy_version,schema_version,provider,output_mode)
                    do update set title=excluded.title,description=excluded.description,source_branch=excluded.source_branch,
                    target_source_commit_hash=excluded.target_source_commit_hash,destination_branch=excluded.destination_branch,
                    destination_commit_hash=excluded.destination_commit_hash,merge_base_hash=excluded.merge_base_hash,
                    target_review_key=excluded.target_review_key,target_review_run_id=excluded.target_review_run_id,
                    status='pending',superseded=0,attempts=0,lease_token=null,leased_until=null,
                    running_source_commit_hash=null,running_review_key=null,running_review_run_id=null,
                    error_message=null,updated_at=excluded.updated_at""",
                    (*scope, pr.title, pr.description, pr.source_branch, pr.source_commit_hash, pr.destination_branch,
                     pr.destination_commit_hash, pr.merge_base_hash, policy_version, schema_version, provider,
                     review_key(pr, policy_version, schema_version, provider, "inline_comments"), run_id, now, now))
            if request_comment is not None:
                conn.execute("""insert into processed_pr_comments(workspace,repo_slug,pr_id,comment_id,updated_on,
                    review_requested,processed_at) values(?,?,?,?,?,1,?)""", (*scope, str(request_comment[0]), request_comment[1], now))
            return self._round(conn, conn.execute("select * from inline_rounds where id=?", (round_id,)).fetchone())

    def _close_barrier(self, conn, round_id, now):
        outcomes = conn.execute("select status from inline_round_providers where round_id=?", (round_id,)).fetchall()
        if any(row["status"] == "pending" for row in outcomes):
            return
        status = "ready_for_selection" if any(row["status"] == "succeeded" for row in outcomes) else "review_failed"
        conn.execute("""update inline_rounds set status=?,selection_ready_at=?,version=version+1,updated_at=?
            where id=? and status='reviewing'""", (status, now, now, round_id))

    def save_result(self, job, result: dict) -> bool:
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            member = conn.execute("""select p.* from inline_round_providers p join inline_rounds r on r.id=p.round_id
                join review_jobs j on j.target_review_run_id=p.run_id
                where p.run_id=? and p.status='pending' and r.status='reviewing' and j.id=?
                and j.status='running' and j.superseded=0 and j.lease_token=?
                and j.running_review_run_id=p.run_id and j.leased_until>?""",
                (job.running_review_run_id, job.id, job.lease_token, now)).fetchone()
            if member is None:
                return False
            if member["recovery_deadline_at"] is not None and member["recovery_deadline_at"] <= now:
                self._fail_provider(conn, member["round_id"], job.provider, "provider_recovery_expired", now)
                return False
            conn.execute("""update inline_round_providers set status='succeeded',result=?,completed_at=?
                where round_id=? and provider=?""", (_json(result), now, member["round_id"], job.provider))
            conn.execute("""update review_jobs set status='reviewed',lease_token=null,leased_until=null,updated_at=?
                where id=?""", (now, job.id))
            self._close_barrier(conn, member["round_id"], now)
            return True

    def _fail_provider(self, conn, round_id, provider, reason, now):
        changed = conn.execute("""update inline_round_providers set status='failed',error=?,completed_at=?
            where round_id=? and provider=? and status='pending' and exists
            (select 1 from inline_rounds where id=? and status='reviewing')""",
            (reason, now, round_id, provider, round_id)).rowcount
        if changed:
            conn.execute("""update review_jobs set status='cancelled',superseded=1,lease_token=null,
                leased_until=null,error_message=?,updated_at=? where target_review_run_id in
                (select run_id from inline_round_providers where round_id=? and provider=?)""",
                (reason, now, round_id, provider))
            self._close_barrier(conn, round_id, now)
        return bool(changed)

    def fail_provider(self, round_id: str, provider: str, reason: str) -> bool:
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            return self._fail_provider(conn, round_id, provider, reason, utcnow())

    def start_provider_recovery(self, round_id: str, provider: str, seconds: int) -> Optional[str]:
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            conn.execute("""update inline_round_providers set recovery_deadline_at=coalesce(recovery_deadline_at,?)
                where round_id=? and provider=? and status='pending' and exists
                (select 1 from inline_rounds where id=? and status='reviewing')""",
                (_after(utcnow(), seconds), round_id, provider, round_id))
            row = conn.execute("select recovery_deadline_at from inline_round_providers where round_id=? and provider=?", (round_id, provider)).fetchone()
            return row[0] if row else None

    def expire_provider_recovery(self) -> int:
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            rows = conn.execute("""select p.round_id,p.provider from inline_round_providers p join inline_rounds r
                on r.id=p.round_id where p.status='pending' and p.recovery_deadline_at<=? and r.status='reviewing'""", (now,)).fetchall()
            return sum(self._fail_provider(conn, row["round_id"], row["provider"], "provider_recovery_expired", now) for row in rows)

    def _cancel(self, conn, round_id, reason, status, now):
        conn.execute("update inline_rounds set status=?,error=?,version=version+1,lease_token=null,leased_until=null,updated_at=? where id=?", (status, reason, now, round_id))
        conn.execute("""update review_jobs set status='cancelled',superseded=1,lease_token=null,leased_until=null,
            error_message=?,updated_at=? where target_review_run_id in
            (select run_id from inline_round_providers where round_id=?) and status!='succeeded'""", (reason, now, round_id))
        conn.execute("""update inline_publication_intents set status=case when status='sending' or uncertain=1 then 'unknown' else 'cancelled' end,
            version=version+1,error=?,updated_at=? where round_id=? and status in ('ready','sending')""", (reason, now, round_id))

    def cancel_round(self, round_id: str, reason: str) -> bool:
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select status from inline_rounds where id=?", (round_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                return False
            self._cancel(conn, round_id, reason, "cancelled", utcnow())
            return True

    def cancel_pr(self, conn, workspace, repo_slug, pr_id, reason):
        rows = conn.execute("select id,status from inline_rounds where workspace=? and repo_slug=? and pr_id=?", (workspace, repo_slug, pr_id)).fetchall()
        for row in rows:
            if row["status"] in ACTIVE:
                self._cancel(conn, row["id"], reason, "cancelled", utcnow())

    def claim_round(self, round_id: str, lease_seconds: int) -> Optional[dict]:
        now, token = utcnow(), uuid.uuid4().hex
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            changed = conn.execute("""update inline_rounds set lease_token=?,leased_until=?,version=version+1,
                status=case when exists(select 1 from inline_selection_plans where round_id=inline_rounds.id)
                then 'publishing' else 'selecting' end,updated_at=? where id=?
                and status in ('ready_for_selection','selecting','publishing')
                and (lease_token is null or leased_until<=?) and (retry_after is null or retry_after<=?)""",
                (token, _after(now, lease_seconds), now, round_id, now, now)).rowcount
            if not changed:
                return None
            return self._round(conn, conn.execute("select * from inline_rounds where id=?", (round_id,)).fetchone())

    def renew_round(self, round_id: str, lease_token: str, lease_seconds: int) -> bool:
        now = utcnow()
        with self.store.connect() as conn:
            return conn.execute("""update inline_rounds set leased_until=?,updated_at=? where id=? and lease_token=?
                and leased_until>? and status in ('selecting','publishing')""",
                (_after(now, lease_seconds), now, round_id, lease_token, now)).rowcount == 1

    def release_round(self, round_id: str, lease_token: str, status=None, error=None, backoff_seconds=0) -> bool:
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            changed = conn.execute("""update inline_rounds set status=coalesce(?,status),lease_token=null,leased_until=null,
                retry_after=?,error=?,attempts=attempts+?,version=version+1,updated_at=?
                where id=? and lease_token=? and leased_until>? and status in ('selecting','publishing')""",
                (status, _after(now, backoff_seconds) if backoff_seconds else None, error, int(error is not None), now, round_id, lease_token, now)).rowcount
            if changed and status in ("completed", "completed_with_stale_findings"):
                conn.execute("""update review_jobs set status='succeeded',updated_at=? where status='reviewed'
                    and target_review_run_id in (select run_id from inline_round_providers where round_id=?)""", (now, round_id))
            return changed == 1

    def selection_failed(self, round_id: str, lease_token: str, error: str,
                         permanent: bool = False, backoff_seconds: int = 0) -> bool:
        now = utcnow()
        with self.store.connect() as conn:
            return conn.execute("""update inline_rounds set status='ready_for_selection',
                selection_attempts=selection_attempts+1,error=?,lease_token=null,leased_until=null,
                retry_after=?,selection_recovery_deadline_at=case when ? then ? else selection_recovery_deadline_at end,
                version=version+1,updated_at=? where id=? and lease_token=? and leased_until>?
                and status='selecting'""",
                (error, _after(now, backoff_seconds) if backoff_seconds and not permanent else None,
                 int(permanent), now, now, round_id, lease_token, now)).rowcount == 1

    def publication_failed(self, round_id: str, lease_token: str, error: str,
                           max_attempts: int, backoff_seconds: int = 0) -> bool:
        now = utcnow()
        with self.store.connect() as conn:
            return conn.execute("""update inline_rounds set status=case when attempts+1>=?
                then 'publication_failed' else status end,attempts=attempts+1,error=?,lease_token=null,
                leased_until=null,retry_after=?,version=version+1,updated_at=? where id=? and lease_token=?
                and leased_until>? and status in ('selecting','publishing')""",
                (max_attempts, error, _after(now, backoff_seconds) if backoff_seconds else None,
                 now, round_id, lease_token, now)).rowcount == 1

    def start_selection_recovery(self, round_id: str, seconds: int) -> Optional[str]:
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            conn.execute("""update inline_rounds set selection_recovery_deadline_at=coalesce(selection_recovery_deadline_at,?)
                where id=? and status in ('ready_for_selection','selecting')""", (_after(utcnow(), seconds), round_id))
            row = conn.execute("select selection_recovery_deadline_at from inline_rounds where id=?", (round_id,)).fetchone()
            return row[0] if row else None

    def save_plan(self, round_id: str, lease_token: str, plan: dict, kind: str = "model") -> bool:
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("""select * from inline_rounds where id=? and lease_token=? and leased_until>?
                and status in ('selecting','ready_for_selection')""", (round_id, lease_token, now)).fetchone()
            if row is None or (kind == "model" and row["selection_recovery_deadline_at"] is not None and row["selection_recovery_deadline_at"] <= now):
                return False
            inserted = conn.execute("insert or ignore into inline_selection_plans(round_id,kind,plan,created_at) values(?,?,?,?)", (round_id, kind, _json(plan), now)).rowcount
            if inserted:
                conn.execute("update inline_rounds set status='publishing',version=version+1,updated_at=? where id=?", (now, round_id))
            return inserted == 1

    def get_plan(self, round_id: str) -> Optional[dict]:
        with self.store.connect() as conn:
            row = conn.execute("select plan from inline_selection_plans where round_id=?", (round_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _intent(self, row):
        if row is None:
            return None
        item = dict(row)
        item["payload"], item["comment_ids"] = json.loads(item["payload"]), json.loads(item["comment_ids"])
        item["stale"] = bool(item["stale"])
        item["uncertain"] = bool(item["uncertain"])
        return item

    def reserve_intent(self, round_id: str, key: str, payload: dict, kind: str = "finding") -> Optional[dict]:
        now, intent_id = utcnow(), uuid.uuid4().hex
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select status from inline_rounds where id=?", (round_id,)).fetchone()
            if row is None or row["status"] != "publishing":
                return None
            marker = payload.get("marker") or "<!-- scout-publication:{} -->".format(intent_id)
            conn.execute("""insert or ignore into inline_publication_intents
                (id,round_id,intent_key,kind,marker,payload,created_at,updated_at) values(?,?,?,?,?,?,?,?)""",
                (intent_id, round_id, key, kind, marker, _json(payload), now, now))
            return self._intent(conn.execute("select * from inline_publication_intents where round_id=? and intent_key=?", (round_id, key)).fetchone())

    def get_intent(self, intent_id: str) -> Optional[dict]:
        with self.store.connect() as conn:
            return self._intent(conn.execute("select * from inline_publication_intents where id=?", (intent_id,)).fetchone())

    def list_intents(self, round_id=None, workspace=None, repo_slug=None, pr_id=None, statuses=None) -> list:
        clauses, params = [], []
        for key, value in (("i.round_id", round_id), ("r.workspace", workspace), ("r.repo_slug", repo_slug), ("r.pr_id", pr_id)):
            if value is not None:
                clauses.append(key + "=?")
                params.append(value)
        if statuses is not None:
            if not statuses:
                return []
            clauses.append("i.status in ({})".format(",".join("?" for _ in statuses)))
            params.extend(statuses)
        sql = "select i.*,r.workspace,r.repo_slug,r.pr_id from inline_publication_intents i join inline_rounds r on r.id=i.round_id"
        if clauses:
            sql += " where " + " and ".join(clauses)
        with self.store.connect() as conn:
            return [self._intent(row) for row in conn.execute(sql + " order by i.created_at,i.id", params).fetchall()]

    def transition_intent(self, intent_id: str, expected_version: int, status: str, **changes) -> Optional[dict]:
        allowed = {"ready": ("sending", "cancelled", "unknown"), "sending": ("published", "unknown"),
                   "unknown": ("ready", "published", "cancelled", "unknown"), "published": ("published",), "cancelled": ("cancelled",)}
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select * from inline_publication_intents where id=? and version=?", (intent_id, expected_version)).fetchone()
            if row is None or status not in allowed[row["status"]]:
                return None
            if status == "cancelled" and row["uncertain"]:
                return None
            if status in ("ready", "sending"):
                round_ = conn.execute("select * from inline_rounds where id=?", (row["round_id"],)).fetchone()
                if round_["status"] != "publishing" or row["stale"]:
                    return None
                if status == "sending" and (not round_["lease_token"] or round_["leased_until"] <= now):
                    return None
                if changes.get("lease_token") is not None and changes["lease_token"] != round_["lease_token"]:
                    return None
            fields = {"status": status, "version": expected_version + 1, "updated_at": now}
            for key, value in changes.items():
                if key not in ("comment_ids", "author_id", "error", "stale", "lease_token"):
                    raise ValueError("Unsupported intent field: " + key)
                fields[key] = _json([str(i) for i in value]) if key == "comment_ids" else value
            if status == "sending":
                fields.update(attempts=row["attempts"] + 1, last_attempt_at=now, uncertain=1)
            elif status == "published":
                fields["uncertain"] = 0
            conn.execute("update inline_publication_intents set {} where id=? and version=?".format(",".join(key + "=?" for key in fields)), (*fields.values(), intent_id, expected_version))
            return self._intent(conn.execute("select * from inline_publication_intents where id=?", (intent_id,)).fetchone())

    def mark_candidate(self, round_id: str, candidate_id: str, status: str, target_id=None) -> None:
        with self.store.connect() as conn:
            conn.execute("""insert into inline_candidate_outcomes(round_id,candidate_id,status,target_id) values(?,?,?,?)
                on conflict(round_id,candidate_id) do update set status=excluded.status,target_id=excluded.target_id
                where inline_candidate_outcomes.status not in ('stale_unpublished','cancelled')""", (round_id, candidate_id, status, target_id))

    def candidate_outcomes(self, round_id: str) -> dict:
        with self.store.connect() as conn:
            return {r["candidate_id"]: {"status": r["status"], "target_id": r["target_id"]}
                    for r in conn.execute("select * from inline_candidate_outcomes where round_id=?", (round_id,))}

    def history(self, workspace: str, repo_slug: str, pr_id: int) -> list:
        with self.store.connect() as conn:
            return [dict(metadata=json.loads(r["metadata"]), comment_id=r["comment_id"], finding=json.loads(r["finding"]))
                    for r in conn.execute("select * from inline_finding_history where workspace=? and repo_slug=? and pr_id=? order by comment_id", (workspace, repo_slug, pr_id))]

    def upsert_history(self, workspace: str, repo_slug: str, pr_id: int, comment_id: str, finding: dict, **metadata) -> None:
        with self.store.connect() as conn:
            conn.execute("""insert into inline_finding_history(workspace,repo_slug,pr_id,comment_id,finding,metadata)
                values(?,?,?,?,?,?) on conflict(workspace,repo_slug,pr_id,comment_id) do update set
                finding=excluded.finding,metadata=excluded.metadata""", (workspace, repo_slug, pr_id, str(comment_id), _json(finding), _json(metadata)))

    def remember_identity(self, identity: str, source: str) -> None:
        with self.store.connect() as conn:
            conn.execute("insert or ignore into inline_bot_identities(identity,source,verified_at) values(?,?,?)", (identity, source, utcnow()))

    def verified_identities(self) -> list:
        with self.store.connect() as conn:
            return [r[0] for r in conn.execute("select identity from inline_bot_identities order by identity")]

    def record_anomaly(self, intent_id: str, comment_ids: list, error: str) -> None:
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select comment_ids from inline_publication_intents where id=?", (intent_id,)).fetchone()
            if row:
                ids = sorted({str(i) for i in json.loads(row[0])} | {str(i) for i in comment_ids})
                conn.execute("update inline_publication_intents set comment_ids=?,error=?,version=version+1,updated_at=? where id=?", (_json(ids), error, utcnow(), intent_id))

    def list_unresolved_publications(self) -> dict:
        return {"intents": self.list_intents(statuses=("unknown", "sending")), "rounds": self.list_rounds(statuses=("publication_failed",))}

    def resolve_publication(self, intent_id: str, expected_version: int, outcome: str, comment_id=None) -> bool:
        if outcome not in ("published", "absent") or (outcome == "published" and not comment_id):
            raise ValueError("Published resolution requires a comment ID; outcome must be published or absent")
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select * from inline_publication_intents where id=? and version=? and status='unknown'", (intent_id, expected_version)).fetchone()
            if row is None:
                return False
            round_ = conn.execute("select status from inline_rounds where id=?", (row["round_id"],)).fetchone()
            error, ids = None, json.loads(row["comment_ids"])
            if outcome == "published":
                status, ids = "published", sorted(set(ids + [str(comment_id)]))
            elif round_["status"] not in PUBLISHABLE + ("publication_failed",):
                status, error = "cancelled", "operator_confirmed_absent_round_ineligible"
            elif row["stale"]:
                status, error = "cancelled", "operator_confirmed_absent_stale"
                payload = json.loads(row["payload"])
                for candidate_id in [payload.get("candidate_id") or payload.get("finding", {}).get("id")] + payload.get("covers", []):
                    if candidate_id:
                        conn.execute("""insert into inline_candidate_outcomes(round_id,candidate_id,status)
                            values(?,?,'stale_unpublished') on conflict(round_id,candidate_id) do update set status='stale_unpublished'""", (row["round_id"], candidate_id))
            else:
                status = "ready"
            conn.execute("""update inline_publication_intents set status=?,comment_ids=?,error=?,uncertain=0,version=version+1,
                updated_at=? where id=? and version=?""", (status, _json(ids), error, now, intent_id, expected_version))
            conn.execute("""insert into inline_publication_audit(intent_id,previous_version,outcome,comment_id,created_at)
                values(?,?,?,?,?)""", (intent_id, expected_version, outcome, str(comment_id) if comment_id else None, now))
            return True

    def retry_publication(self, round_id: str) -> bool:
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            changed = conn.execute("""update inline_rounds set status=case when exists
                (select 1 from inline_selection_plans where round_id=inline_rounds.id) then 'publishing' else 'ready_for_selection' end,
                attempts=0,retry_after=null,lease_token=null,leased_until=null,error=null,version=version+1,updated_at=?
                where id=? and status='publication_failed'""", (utcnow(), round_id)).rowcount == 1
            if changed:
                conn.execute("""update inline_publication_intents set attempts=0,version=version+1,updated_at=?
                    where round_id=? and status='ready' and stale=0 and uncertain=0""", (utcnow(), round_id))
            return changed

    def finish_closed_pr(self, workspace: str, repo_slug: str, pr_id: int) -> None:
        """Called only after the publisher's final reconciliation without resending."""
        now = utcnow()
        with self.store.connect() as conn:
            conn.execute("begin immediate")
            self.cancel_pr(conn, workspace, repo_slug, pr_id, "Pull request closed")
            # Closing a PR ends delivery; cancellation does not assert remote absence.
            conn.execute("""update inline_publication_intents set status='cancelled',version=version+1,
                error='pr_closed_outcome_unconfirmed',updated_at=? where status in ('sending','unknown','ready')
                and round_id in(select id from inline_rounds where workspace=? and repo_slug=? and pr_id=?)""",
                (now, workspace, repo_slug, pr_id))
            scope = (workspace, repo_slug, pr_id)
            for table in ("inline_candidate_outcomes", "inline_publication_intents", "inline_selection_plans", "inline_round_providers"):
                if table == "inline_publication_intents":
                    conn.execute("""delete from inline_publication_audit where intent_id in(select i.id
                        from inline_publication_intents i join inline_rounds r on r.id=i.round_id
                        where r.workspace=? and r.repo_slug=? and r.pr_id=?)""", scope)
                conn.execute("delete from {} where round_id in(select id from inline_rounds where workspace=? and repo_slug=? and pr_id=?)".format(table), scope)
            conn.execute("delete from inline_rounds where workspace=? and repo_slug=? and pr_id=?", scope)
            conn.execute("delete from inline_finding_history where workspace=? and repo_slug=? and pr_id=?", scope)
