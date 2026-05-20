from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .config import ClaudeConfig, CredentialStore
from .provider import (
    PROVIDER_COOLDOWN_STATUS,
    ProviderError,
    ProviderResult,
    ProviderSuperseded,
    provider_quota_cooldown_seconds,
    read_text as _read_text,
    redacted_cmd as _redacted_cmd,
    terminate_process_group as _terminate_process_group,
)
from .risk import build_risk_prompt, extract_risk, risk_schema_json
from .usage import parse_claude_usage

LOG = logging.getLogger(__name__)
CLAUDE_READONLY_TOOLS = "Task,Read,Grep,Glob"
CLAUDE_DENIED_TOOLS = "Bash,Edit,MultiEdit,Write,NotebookEdit,WebFetch,WebSearch"


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
    ) -> ProviderResult:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        stdout_file = Path(run_dir) / "claude-stdout.log"
        stderr_file = Path(run_dir) / "claude-stderr.log"
        prompt_file = Path(run_dir) / "claude-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        schema_content = self._read_schema(schema_path)
        cmd = self.build_command(prompt, schema_content)
        LOG.info("starting Claude review command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        with prompt_file.open("r", encoding="utf-8") as prompt_input, \
            stdout_file.open("w", encoding="utf-8") as stdout, \
            stderr_file.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                cmd,
                stdin=prompt_input,
                stdout=stdout,
                stderr=stderr,
                cwd=worktree,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
            started = time.monotonic()
            while True:
                if proc.poll() is not None:
                    break
                if is_superseded():
                    _terminate_process_group(proc)
                    raise ProviderSuperseded("review superseded by a newer PR commit")
                if time.monotonic() - started > self.config.timeout_seconds:
                    _terminate_process_group(proc)
                    stdout.flush()
                    stderr.flush()
                    cooldown_seconds = provider_quota_cooldown_seconds(
                        "claude",
                        _read_text(stdout_file),
                        _read_text(stderr_file),
                    )
                    raise ProviderError(
                        "Claude review timed out after {} seconds".format(self.config.timeout_seconds),
                        cooldown_seconds=cooldown_seconds,
                        provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
                    )
                time.sleep(1)

        stdout_text = _read_text(stdout_file)
        stderr_text = _read_text(stderr_file)
        cooldown_seconds = provider_quota_cooldown_seconds("claude", stdout_text, stderr_text)
        LOG.info(
            "Claude review completed returncode=%s stdout_file=%s stderr_file=%s stdout_present=%s",
            proc.returncode,
            stdout_file,
            stderr_file,
            bool(stdout_text.strip()),
        )
        if proc.returncode != 0:
            raise ProviderError(
                "Claude exited with status {}: {}".format(proc.returncode, stderr_text[:1000]),
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        if not stdout_text.strip():
            raise ProviderError(
                "Claude did not write final JSON to stdout",
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        final_message = _extract_final_message(stdout_text)
        return ProviderResult(
            stdout=stdout_text,
            stderr=stderr_text,
            final_message=final_message,
            usage=parse_claude_usage(stdout_text),
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
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        stdout_file = Path(run_dir) / "claude-risk-stdout.log"
        stderr_file = Path(run_dir) / "claude-risk-stderr.log"
        prompt_file = Path(run_dir) / "claude-risk-prompt.txt"
        prompt_file.write_text(build_risk_prompt(description), encoding="utf-8")

        cmd = self.build_risk_command(risk_schema_json(), model, effort)
        LOG.info("starting Claude risk command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        with prompt_file.open("r", encoding="utf-8") as prompt_input, \
            stdout_file.open("w", encoding="utf-8") as stdout, \
            stderr_file.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                cmd,
                stdin=prompt_input,
                stdout=stdout,
                stderr=stderr,
                cwd=run_dir,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
            started = time.monotonic()
            while True:
                if proc.poll() is not None:
                    break
                if is_superseded():
                    _terminate_process_group(proc)
                    raise ProviderSuperseded("review superseded by a newer PR commit")
                if time.monotonic() - started > timeout_seconds:
                    _terminate_process_group(proc)
                    stdout.flush()
                    stderr.flush()
                    cooldown_seconds = provider_quota_cooldown_seconds(
                        "claude",
                        _read_text(stdout_file),
                        _read_text(stderr_file),
                    )
                    raise ProviderError(
                        "Claude risk classification timed out after {} seconds".format(timeout_seconds),
                        cooldown_seconds=cooldown_seconds,
                        provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
                    )
                time.sleep(1)

        stdout_text = _read_text(stdout_file)
        stderr_text = _read_text(stderr_file)
        cooldown_seconds = provider_quota_cooldown_seconds("claude", stdout_text, stderr_text)
        LOG.info(
            "Claude risk completed returncode=%s stdout_file=%s stderr_file=%s stdout_present=%s",
            proc.returncode,
            stdout_file,
            stderr_file,
            bool(stdout_text.strip()),
        )
        if proc.returncode != 0:
            raise ProviderError(
                "Claude risk classification exited with status {}: {}".format(proc.returncode, stderr_text[:1000]),
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        if not stdout_text.strip():
            raise ProviderError(
                "Claude risk classification did not write final JSON to stdout",
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        return extract_risk(stdout_text)

    def build_command(self, prompt: str, schema_content: str) -> list:
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
        return cmd

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
