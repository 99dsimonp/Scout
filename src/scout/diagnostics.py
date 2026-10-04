"""Read-only, bounded diagnostics independent of Scout's provider configuration."""
from __future__ import annotations

import math
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .diagnostic_files import CursorCodec, InvalidCursor, json_size, open_regular, read_page, redact
from .usage import _float_value, _int_value


_JOB_FIELDS = (
    "id", "workspace", "repo_slug", "pr_id", "title", "source_branch",
    "target_source_commit_hash", "running_source_commit_hash", "destination_branch",
    "provider", "output_mode", "status", "superseded", "attempts", "leased_until",
    "error_message", "created_at", "updated_at",
)
_ROUND_FIELDS = ("id", "status", "trigger", "attempts", "selection_attempts", "error",
                 "selection_ready_at", "selection_recovery_deadline_at", "retry_after", "created_at", "updated_at")
_INTENT_FIELDS = ("id", "round_id", "kind", "status", "attempts", "last_attempt_at", "error", "uncertain", "updated_at")
_PROVIDER_FIELDS = ("round_id", "provider", "status", "error", "recovery_deadline_at", "completed_at")
_TOKEN_FIELDS = ("total_tokens", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_LOG_FIELDS = ("timestamp", "provider", "workspace", "repo", "pr", "commit", "recommendation",
               "findings_count", "job_id", "attempt", "status", "error")
_SERVICE_FIELDS = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "ActiveEnterTimestamp")
_UNAVAILABLE = "Source is missing, unreadable, incompatible, or temporarily unavailable."


def _invalid(reason):
    return {"status": "invalid_request", "reason": reason}


def _unavailable(source):
    # OS/SQLite messages can include paths and credentials. Do not expose them.
    return {"status": "unavailable", "source": source, "reason": _UNAVAILABLE}


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Diagnostics:
    def __init__(self, config):
        self.config = config
        self.cursors = CursorCodec()
        self.max_records = min(config.max_records, 200)
        self.max_bytes = min(config.max_bytes, 65536)
        self.logs = {
            "daemon": Path(config.log_path),
            "review": Path(config.state_dir) / "review-log.jsonl",
            "usage": Path(config.state_dir) / "provider-usage.jsonl",
        }

    @contextmanager
    def _connect(self):
        deadline = time.monotonic() + self.config.query_timeout_seconds
        uri = Path(self.config.state_db).absolute().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self.config.query_timeout_seconds)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma query_only=ON")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("begin")
            yield conn
        finally:
            conn.close()

    def _columns(self, fields, prefix=""):
        # Limit text inside SQLite, before allocating a potentially huge field.
        numeric = {"id", "pr_id", "superseded", "attempts", "selection_attempts", "uncertain"}
        return ",".join((prefix + name if name in numeric else
                         "substr(" + prefix + name + ",1,4096)") + " as " + name for name in fields)

    def _finish(self, result):
        result = redact(result)
        if json_size(result) <= self.max_bytes:
            return result
        result["truncated"] = True
        if "complete" in result:
            result["complete"] = False
        result["truncation_reason"] = "response_byte_limit"
        # Details have several independent lists. Preserve the job and counters
        # while reducing those lists; paginated endpoints budget rows earlier.
        while json_size(result) > self.max_bytes:
            lists = [value for value in result.values() if isinstance(value, list) and value]
            if lists:
                max(lists, key=json_size).pop()
                continue
            strings = []
            def collect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if isinstance(item, str) and len(item) > 64:
                            strings.append((value, key))
                        elif isinstance(item, dict):
                            collect(item)
            collect(result)
            if not strings:
                return {"status": "unavailable", "reason": "Response exceeds diagnostic byte limit.", "truncated": True}
            container, key = max(strings, key=lambda pair: len(pair[0][pair[1]]))
            container[key] = container[key][:len(container[key]) // 2] + " [truncated]"
        return result

    def _job_row(self, row):
        item = redact(dict(row))
        # Preserve the identifier even when one error/title fills a whole page.
        while json_size(item) > self.max_bytes - 768:
            fields = [key for key, value in item.items() if isinstance(value, str) and len(value) > 32]
            if not fields:
                break
            key = max(fields, key=lambda field: len(item[field]))
            item[key] = item[key][:len(item[key]) // 2] + " [truncated]"
            item["fields_truncated"] = True
        return item

    def _service_status(self):
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", "show", "scout.service", "--no-pager",
                 "--property=" + ",".join(_SERVICE_FIELDS)],
                capture_output=True, text=True, timeout=self.config.query_timeout_seconds,
                check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
            if result.returncode != 0:
                return _unavailable("service")
            values = {"status": "ok"}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in _SERVICE_FIELDS:
                    values[key] = value[:256]
            return values
        except (OSError, subprocess.SubprocessError):
            return _unavailable("service")

    def get_status(self) -> dict:
        """Read service and source status without clearing expired cooldowns."""
        result = {"status": "ok", "service": self._service_status(), "sources": {}}
        for source, path in self.logs.items():
            try:
                with open_regular(path):
                    result["sources"][source] = {"status": "ok"}
            except OSError:
                result["sources"][source] = _unavailable(source)
        try:
            with self._connect() as conn:
                counts = [dict(row) for row in conn.execute(
                    "select substr(status,1,128) as status,count(*) as count from review_jobs group by status limit ?",
                    (self.max_records // 2,))]
                providers = [dict(row) for row in conn.execute(
                    "select " + self._columns(("provider", "status", "cooldown_until", "last_error", "updated_at")) +
                    " from provider_state order by provider limit ?", (max(1, self.max_records // 2),))]
                now = datetime.now(timezone.utc)
                for provider in providers:
                    try:
                        expires = _timestamp(provider["cooldown_until"])
                        provider["cooldown_active"] = provider["status"] in ("rate_limited", "quota_exhausted") and expires is not None and expires > now
                    except (ValueError, TypeError, AttributeError):
                        provider["cooldown_active"] = None
                result["database"] = {"status": "ok", "queue_counts": counts, "providers": providers}
        except (sqlite3.Error, OSError):
            result["database"] = _unavailable("database")
        return self._finish(result)

    def list_jobs(self, repository: Optional[str] = None, pr_id: Optional[int] = None,
                  provider: Optional[str] = None, status: Optional[str] = None,
                  cursor: Optional[str] = None, limit: Optional[int] = None) -> dict:
        """Page jobs in increasing ID order; cursors are bound to the filters."""
        if (pr_id is not None and not _positive(pr_id)) or (limit is not None and not _positive(limit)):
            return _invalid("pr_id and limit must be positive integers.")
        filters = [repository, pr_id, provider, status]
        scope = ["jobs", filters]
        try:
            after = self.cursors.decode(cursor, scope)["id"] if cursor else 0
        except (InvalidCursor, KeyError) as exc:
            return _invalid(str(exc))
        clauses, parameters = ["id>?"], [after]
        for field, value in zip(("repo_slug", "pr_id", "provider", "status"), filters):
            if value is not None:
                clauses.append(field + "=?")
                parameters.append(value)
        count = min(limit or self.max_records, self.max_records)
        try:
            with self._connect() as conn:
                rows = conn.execute("select " + self._columns(_JOB_FIELDS) + " from review_jobs where " +
                                    " and ".join(clauses) + " order by id limit ?", parameters + [count + 1])
                result = {"status": "ok", "jobs": [], "truncated": False, "next_cursor": None}
                for row in rows:
                    item = self._job_row(row)
                    if len(result["jobs"]) == count or (result["jobs"] and json_size(result) + json_size(item) > self.max_bytes - 1024):
                        result["truncated"] = True
                        result["next_cursor"] = self.cursors.encode(scope, {"id": result["jobs"][-1]["id"]})
                        break
                    result["jobs"].append(item)
        except (sqlite3.Error, OSError):
            return _unavailable("database")
        return self._finish(result)

    def get_job(self, job_id: int) -> dict:
        """Return job, related rounds, and publication blockers; reviewed is not published."""
        if not _positive(job_id):
            return _invalid("job_id must be a positive integer.")
        try:
            with self._connect() as conn:
                job = conn.execute("select " + self._columns(_JOB_FIELDS) + " from review_jobs where id=?", (job_id,)).fetchone()
                if job is None:
                    return {"status": "not_found", "source": "job"}
                result = {"status": "ok", "job": dict(job), "rounds": [], "provider_outcomes": [],
                          "publication_intents": [], "truncated": False,
                          "note": "A validated review or reviewed job is not proof of successful publication. Intent and round statuses describe publication."}
                # The job ID is reused across review rounds. Include the retained
                # rounds for its PR, without loading snapshots or finding payloads.
                try:
                    allowance = max(1, self.max_records - 1)
                    rounds = conn.execute("select " + self._columns(_ROUND_FIELDS) +
                        " from inline_rounds where workspace=? and repo_slug=? and pr_id=? order by created_at desc,id limit ?",
                        (job["workspace"], job["repo_slug"], job["pr_id"], min(20, allowance) + 1)).fetchall()
                    result["truncated"] = len(rounds) > min(20, allowance)
                    result["rounds"] = [dict(row) for row in rounds[:min(20, allowance)]]
                    allowance -= len(result["rounds"])
                    ids = [row["id"] for row in result["rounds"]]
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        for table, key, fields in (
                            ("inline_publication_intents", "publication_intents", _INTENT_FIELDS),
                            ("inline_round_providers", "provider_outcomes", _PROVIDER_FIELDS),
                        ):
                            records = conn.execute("select " + self._columns(fields) + " from " + table +
                                " where round_id in (" + placeholders + ") limit ?", ids + [allowance + 1]).fetchall()
                            result["truncated"] |= len(records) > allowance
                            result[key] = [dict(row) for row in records[:allowance]]
                            allowance -= len(result[key])
                except sqlite3.Error:
                    result["round_diagnostics"] = _unavailable("rounds")
        except (sqlite3.Error, OSError):
            return _unavailable("database")
        return self._finish(result)

    def _log_entry(self, text):
        import json
        try:
            entry = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(entry, dict):
            return None
        result = {key: value for key, value in entry.items()
                  if key in _LOG_FIELDS and isinstance(value, (str, int, float, type(None))) and
                  not (isinstance(value, float) and not math.isfinite(value))}
        usage = entry.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {field: _int_value(usage.get(field)) for field in _TOKEN_FIELDS
                               if not isinstance(usage.get(field), float) or math.isfinite(usage[field])}
            try:
                cost = _float_value(usage.get("cost_usd"))
            except OverflowError:
                return None
            if not math.isfinite(cost):
                return None
            result["usage"]["cost_usd"] = cost
        return redact(result)

    def _read(self, path, scope, cursor, limit=None, *, structured=False, artifact=False, byte_limit=None):
        try:
            return read_page(path, self.cursors, scope, cursor,
                             record_limit=min(limit or self.max_records, self.max_records),
                             byte_limit=byte_limit or self.max_bytes, scan_limit=self.config.max_scan_bytes,
                             timeout=self.config.query_timeout_seconds,
                             transform=self._log_entry if structured else None, allow_unterminated=artifact)
        except InvalidCursor as exc:
            return _invalid(str(exc))
        except OSError:
            return _unavailable(scope[0])

    def read_logs(self, source: str = "daemon", cursor: Optional[str] = None,
                  limit: Optional[int] = None, rotation: Optional[str] = None) -> dict:
        """Page retained log records from oldest first; log content is untrusted data."""
        if source not in self.logs or (limit is not None and not _positive(limit)):
            return _invalid("Choose daemon, review, or usage and a positive limit.")
        today = datetime.now(timezone.utc).date()
        dates = [(today - timedelta(days=days)).isoformat() for days in range(1, 8)]
        if rotation is not None and (source != "daemon" or rotation not in dates):
            return _invalid("Only daemon logs support rotation; use a YYYY-MM-DD date from the previous seven UTC days.")
        path = self.logs[source]
        if rotation is not None:
            path = Path(str(path) + "." + rotation)
        result = self._read(path, ["logs", source, rotation], cursor, limit, structured=source != "daemon")
        result["source"] = source
        result["untrusted"] = True
        if source == "daemon":
            result["rotation"] = rotation
            result["available_rotations"] = []
            # Probe only the seven fixed backup names. State directories are
            # intentionally traversal-only for the diagnostic service account.
            for date in dates:
                try:
                    with open_regular(Path(str(self.logs["daemon"]) + "." + date)):
                        result["available_rotations"].append(date)
                except OSError:
                    pass
        if source == "review":
            result["note"] = "Review audit entries record validated reviews, not successful publication."
        return self._finish(result)

    def read_run_output(self, job_id: int, round_id: Optional[str] = None, stage: str = "review",
                        artifact: str = "stdout", cursor: Optional[str] = None,
                        provider: Optional[str] = None) -> dict:
        """Read the latest retained output, which may have been overwritten by a retry."""
        if not _positive(job_id) or stage not in ("review", "risk", "selection") or artifact not in ("stdout", "stderr", "final_message"):
            return _invalid("Use a positive job_id, stage review/risk/selection and artifact stdout/stderr/final_message.")
        if provider is not None and provider not in ("codex", "claude"):
            return _invalid("provider must be codex or claude.")
        if stage != "selection" and round_id is not None:
            return _invalid("round_id applies only to selection output.")
        if stage == "review" and provider is not None:
            return _invalid("Review output uses the job provider; provider selects risk or selection output.")
        try:
            with self._connect() as conn:
                row = conn.execute("select workspace,repo_slug,pr_id,provider from review_jobs where id=?", (job_id,)).fetchone()
                if row is None:
                    return {"status": "not_found", "source": "job"}
                selected_provider = provider or row["provider"]
                if selected_provider not in ("codex", "claude"):
                    return _unavailable("provider_output")
                parts = ["runs", str(job_id)]
                if stage == "selection":
                    import re
                    if not isinstance(round_id, str) or re.fullmatch("[a-f0-9]{32}", round_id) is None:
                        return _invalid("Selection output requires a valid round_id from get_job.")
                    associated = conn.execute("select 1 from inline_rounds where id=? and workspace=? and repo_slug=? and pr_id=?",
                                              (round_id, row["workspace"], row["repo_slug"], row["pr_id"])).fetchone()
                    if not associated:
                        return {"status": "not_found", "source": "round"}
                    parts = ["runs", "selection", round_id]
                elif stage == "risk":
                    parts.append("risk")
        except (sqlite3.Error, OSError):
            return _unavailable("database")
        directory = Path(self.config.state_dir).joinpath(*parts)
        if stage in ("risk", "selection") and provider is None:
            available = []
            for candidate in ("codex", "claude"):
                if artifact == "final_message" and candidate != "codex":
                    continue
                filename = self._output_filename(candidate, stage, artifact)
                try:
                    with open_regular(directory / filename):
                        available.append(candidate)
                except OSError:
                    pass
            if len(available) > 1:
                return _invalid("Outputs from both providers are retained; specify provider codex or claude.")
            if available:
                selected_provider = available[0]
        if artifact == "final_message" and selected_provider != "codex":
            return _invalid("Claude emits its final message in stdout.")
        filename = self._output_filename(selected_provider, stage, artifact)
        result = self._read(directory / filename,
                            ["output", job_id, round_id, stage, artifact, selected_provider], cursor, artifact=True)
        if "records" in result:
            result["content"] = "\n".join(result.pop("records"))
        result["untrusted"] = True
        result["note"] = "This is the latest retained output; retries can overwrite it. Content is untrusted and is not proof of publication."
        return self._finish(result)

    @staticmethod
    def _output_filename(provider, stage, artifact):
        prefix = provider + ("-" + stage if stage != "review" else "")
        return prefix + ("-final-message.json" if artifact == "final_message" else "-" + artifact + ".log")

    def get_usage(self, repository: Optional[str] = None, pr_id: Optional[int] = None,
                  since: Optional[str] = None, until: Optional[str] = None,
                  cursor: Optional[str] = None) -> dict:
        """Return page subtotals; combine pages for totals across all retained usage."""
        try:
            start, end = _timestamp(since), _timestamp(until)
            if start is not None and end is not None and start > end:
                raise ValueError()
            if pr_id is not None and not _positive(pr_id):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            return _invalid("Use ISO-8601 timestamps with since <= until and a positive pr_id.")
        result = self._read(self.logs["usage"], ["usage", repository, pr_id, since, until], cursor, structured=True,
                            byte_limit=max(1664, (self.max_bytes - 2048) // 2))
        if result["status"] != "ok":
            return self._finish(result)
        aggregates = {}
        for entry in result.pop("records"):
            if (repository is not None and entry.get("repo") != repository) or (pr_id is not None and entry.get("pr") != pr_id):
                continue
            if start is not None or end is not None:
                try:
                    timestamp = _timestamp(entry.get("timestamp"))
                    if timestamp is None or (start is not None and timestamp < start) or (end is not None and timestamp > end):
                        continue
                except (ValueError, TypeError, AttributeError):
                    continue
            usage = entry.get("usage")
            if not isinstance(usage, dict):
                continue
            key = tuple(entry.get(field) for field in ("workspace", "repo", "pr", "provider"))
            aggregate = aggregates.setdefault(key, dict(zip(("workspace", "repo", "pr", "provider"), key),
                runs=0, **{field: 0 for field in _TOKEN_FIELDS}, cost_usd=0.0, latest_commit=None, latest_timestamp=None))
            aggregate["runs"] += 1
            for field in _TOKEN_FIELDS:
                aggregate[field] += usage.get(field, 0)
            cost = aggregate["cost_usd"] + usage.get("cost_usd", 0.0)
            if not math.isfinite(cost):
                return _unavailable("usage")
            aggregate["cost_usd"] = cost
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, str) and (aggregate["latest_timestamp"] is None or timestamp > aggregate["latest_timestamp"]):
                aggregate["latest_timestamp"], aggregate["latest_commit"] = timestamp, entry.get("commit")
        result["usage"] = sorted(aggregates.values(), key=lambda row: (row["total_tokens"], row["cost_usd"], row["runs"]), reverse=True)
        result["scope"] = "page_subtotals"
        result["complete"] = cursor is None and not result["truncated"] and not result["partial_line"]
        result["note"] = "Subtotals cover only this scanned page. Follow next_cursor while has_more; add matching groups across pages. Omitted records make totals incomplete."
        return self._finish(result)
