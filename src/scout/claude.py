from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, List, Optional

from .comment_request import (
    CommentRequestClassification,
    build_comment_request_prompt,
    comment_request_schema_json,
    extract_comment_request,
)
from .config import ClaudeConfig, CredentialStore
from .provider import (
    PROVIDER_COOLDOWN_STATUS,
    ProcessOutput,
    ProviderError,
    ProviderResult,
    provider_quota_cooldown_seconds,
    redacted_cmd as _redacted_cmd,
    require_provider_success,
    run_provider_command,
    run_selection_command,
)
from .risk import build_risk_prompt, extract_risk, risk_schema_json
from .usage import parse_claude_usage

LOG = logging.getLogger(__name__)
CLAUDE_READONLY_TOOLS = "Task,Read,Grep,Glob"
CLAUDE_DENIED_TOOLS = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"


class ClaudeRunner:
    def __init__(self, config: ClaudeConfig, credentials: CredentialStore):
        self.config = config
        self.credentials = credentials

    def validate_startup(self) -> None:
        if not self.config.enabled:
            raise ProviderError("Claude provider is disabled", retryable=False)
        if shutil.which(self.config.command) is None:
            raise ProviderError("Claude command not found: {}".format(self.config.command), retryable=False)
        if self.config.auth_mode == "api":
            Path(self.config.home_dir).mkdir(parents=True, exist_ok=True)
            self.credentials.read(self.config.credential)

    def run(
        self,
        worktree: str,
        prompt: str,
        schema_path: str,
        run_dir: str,
        is_superseded: Callable[[], bool],
        additional_dirs: Optional[List[str]] = None,
    ) -> ProviderResult:
        schema_content = self._read_schema(schema_path)
        cmd = self.build_command(prompt, schema_content, additional_dirs=additional_dirs)
        output = self._run_command(
            "review", "claude", "Claude review", cmd, prompt, run_dir, self.config.timeout_seconds,
            is_superseded, "review superseded by a newer PR commit", cwd=worktree,
        )
        require_provider_success(output, "Claude", "Claude did not write final JSON to stdout")
        return ProviderResult(
            stdout=output.stdout,
            stderr=output.stderr,
            final_message=_extract_final_message(output.stdout),
            usage=parse_claude_usage(output.stdout),
        )

    def assess_risk(
        self,
        description: str,
        model: str,
        effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> str:
        label = "Claude risk classification"
        cmd = self.build_risk_command(risk_schema_json(), model, effort)
        output = self._run_command(
            "risk", "claude-risk", label, cmd, build_risk_prompt(description), run_dir, timeout_seconds,
            is_superseded, "review superseded by a newer PR commit",
        )
        require_provider_success(output, label, "{} did not write final JSON to stdout".format(label))
        return extract_risk(output.stdout)

    def classify_review_request(
        self,
        comment: str,
        model: str,
        effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> CommentRequestClassification:
        label = "Claude review request classification"
        cmd = self.build_comment_request_command(comment_request_schema_json(), model, effort)
        output = self._run_command(
            "comment request", "claude-comment-request", label, cmd, build_comment_request_prompt(comment), run_dir, timeout_seconds,
            is_superseded, "review request classification superseded by a newer PR comment",
        )
        require_provider_success(output, label, "{} did not write final JSON to stdout".format(label))
        return extract_comment_request(output.stdout)

    def _run_command(
        self,
        kind: str,
        file_prefix: str,
        label: str,
        cmd: list,
        prompt: str,
        run_dir: str,
        timeout_seconds: int,
        is_superseded: Callable[[], bool],
        superseded_message: str,
        cwd: Optional[str] = None,
    ) -> ProcessOutput:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        stdout_file = Path(run_dir) / "{}-stdout.log".format(file_prefix)
        stderr_file = Path(run_dir) / "{}-stderr.log".format(file_prefix)
        prompt_file = Path(run_dir) / "{}-prompt.txt".format(file_prefix)
        prompt_file.write_text(prompt, encoding="utf-8")
        LOG.info("starting Claude %s command=%s prompt_file=%s", kind, _redacted_cmd(cmd), prompt_file)
        output = run_provider_command(
            "claude", label, cmd, prompt_file, stdout_file, stderr_file, self._env(),
            timeout_seconds, is_superseded, superseded_message, cwd=cwd or run_dir,
        )
        LOG.info(
            "Claude %s completed returncode=%s stdout_file=%s stderr_file=%s stdout_present=%s",
            kind,
            output.returncode,
            stdout_file,
            stderr_file,
            bool(output.stdout.strip()),
        )
        return output

    def build_command(
        self,
        prompt: str,
        schema_content: str,
        additional_dirs: Optional[List[str]] = None,
    ) -> list:
        cmd = [
            self.config.command,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema_content,
            "--tools",
            CLAUDE_READONLY_TOOLS,
            "--allowedTools",
            CLAUDE_READONLY_TOOLS,
            "--disallowedTools",
            CLAUDE_DENIED_TOOLS,
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if self.config.auth_mode == "api":
            cmd.append("--bare")
        if self.config.model.strip():
            cmd.extend(["--model", self.config.model.strip()])
        if self.config.effort.strip():
            cmd.extend(["--effort", self.config.effort.strip()])
        for directory in additional_dirs or []:
            cmd.extend(["--add-dir", directory])
        return cmd

    def classify_findings(
        self,
        prompt: str,
        schema_json: str,
        model: str,
        effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> str:
        run_dir = str(Path(run_dir).resolve())
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        prompt_file = Path(run_dir) / "claude-selection-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        cmd = self.build_comment_request_command(schema_json, model, effort)
        cmd[cmd.index("--tools") + 1] = ""
        cmd[cmd.index("--allowedTools") + 1] = ""
        LOG.info("starting Claude selection command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        stdout = run_selection_command(
            "claude", cmd, prompt_file, run_dir, self._env(), timeout_seconds, is_superseded,
        )
        return _extract_selection_message(stdout)

    def build_risk_command(self, schema_content: str, model: str, effort: str) -> list:
        cmd = [
            self.config.command,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema_content,
            "--tools",
            CLAUDE_READONLY_TOOLS,
            "--allowedTools",
            CLAUDE_READONLY_TOOLS,
            "--disallowedTools",
            CLAUDE_DENIED_TOOLS,
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if self.config.auth_mode == "api":
            cmd.append("--bare")
        if model.strip():
            cmd.extend(["--model", model.strip()])
        if effort.strip():
            cmd.extend(["--effort", effort.strip()])
        return cmd

    def build_comment_request_command(self, schema_content: str, model: str, effort: str) -> list:
        return self.build_risk_command(schema_content, model, effort)

    def _env(self) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        }
        if self.config.auth_mode == "api":
            env["HOME"] = self.config.home_dir
            env["ANTHROPIC_API_KEY"] = self.credentials.read(self.config.credential)
        elif os.environ.get("HOME"):
            env["HOME"] = os.environ["HOME"]
        return env

    def _read_schema(self, schema_path: str) -> str:
        try:
            return Path(schema_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderError(
                "Claude schema file is unreadable: {}".format(schema_path),
                retryable=False,
            ) from exc


def _extract_selection_message(stdout_text: str) -> str:
    cooldown_seconds = provider_quota_cooldown_seconds("claude", stdout_text)
    try:
        parsed = json.loads(stdout_text)
        if not isinstance(parsed, dict):
            raise ValueError("stdout JSON must be an object")
        if parsed.get("is_error") is True:
            raise ValueError("reported an error result")
        result = parsed.get("structured_output", parsed.get("result", parsed))
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ValueError("selection result must be a JSON object")
        return json.dumps(result, separators=(",", ":"))
    except ValueError as exc:
        raise ProviderError(
            "Claude selection {}".format(exc), cooldown_seconds=cooldown_seconds,
            provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
        ) from exc


def _extract_final_message(stdout_text: str) -> str:
    cooldown_seconds = provider_quota_cooldown_seconds("claude", stdout_text)
    try:
        parsed = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "Claude stdout is not valid JSON: {}".format(exc),
            cooldown_seconds=cooldown_seconds,
            provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderError("Claude stdout JSON must be an object")

    if {"recommendation", "report", "annotations"}.issubset(parsed):
        return stdout_text.strip()

    if parsed.get("is_error") is True:
        result = parsed.get("result", "")
        cooldown_seconds = provider_quota_cooldown_seconds("claude", stdout_text, str(result))
        raise ProviderError(
            "Claude reported an error result",
            cooldown_seconds=cooldown_seconds,
            provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
        )
    if "result" not in parsed:
        raise ProviderError("Claude stdout JSON missing result field")

    result = parsed["result"]
    if isinstance(result, str):
        if not result.strip():
            raise ProviderError("Claude result field is empty")
        return _extract_schema_json(result)
    if isinstance(result, (dict, list)):
        return json.dumps(result, separators=(",", ":"))
    raise ProviderError("Claude result field must be a string or JSON value")


def _extract_schema_json(text: str) -> str:
    stripped = text.strip()
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        cooldown_seconds = provider_quota_cooldown_seconds("claude", stripped)
        if cooldown_seconds:
            raise ProviderError(
                "Claude result reported provider usage limit",
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS,
            )
        embedded = _first_schema_json_object(stripped)
        if embedded is not None:
            return embedded
        raise ProviderError("Claude result did not contain a schema JSON object")
    return stripped


def _first_schema_json_object(text: str) -> Optional[str]:
    for candidate in _fenced_json_candidates(text):
        if _is_schema_json_object(candidate):
            return candidate
    for candidate in _json_object_candidates(text):
        if _is_schema_json_object(candidate):
            return candidate
    return None


def _fenced_json_candidates(text: str) -> list:
    candidates = []
    for match in re.finditer(r"```(?:json)?[ \t\r\n]*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        candidate = match.group(1).strip()
        if candidate:
            candidates.append(candidate)
    return candidates


def _json_object_candidates(text: str) -> list:
    candidates = []
    start = text.find("{")
    while start >= 0:
        candidate = _json_object_at(text, start)
        if candidate is not None:
            candidates.append(candidate)
            start = text.find("{", start + len(candidate))
        else:
            start = text.find("{", start + 1)
    return candidates


def _json_object_at(text: str, start: int) -> Optional[str]:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return candidate
    return None


def _is_schema_json_object(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and {"recommendation", "report", "annotations"}.issubset(parsed)
