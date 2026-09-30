from __future__ import annotations

from datetime import datetime, timedelta
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


DEFAULT_PROVIDER_COOLDOWN_SECONDS = 5 * 60 * 60
PROVIDER_COOLDOWN_STATUS = "quota_exhausted"
_COOLDOWN_PROVIDERS = {"claude", "codex"}
_USAGE_LIMIT_PHRASES = (
    "usage limit reached",
    "usage limit has been reached",
    "reached your usage limit",
    "reached the usage limit",
    "hit your usage limit",
    "exceeded your usage limit",
    "5-hour usage limit",
    "5 hour usage limit",
    "five-hour usage limit",
)
_USAGE_LIMIT_CONTEXT = (
    "5-hour",
    "5 hour",
    "five-hour",
    "reset",
    "resets",
    "try again",
)
_USAGE_LIMIT_RESET_TIME_RE = re.compile(
    r"try again at\s+"
    r"(?P<hour>[0-9]{1,2})"
    r"(?::(?P<minute>[0-9]{2})(?::(?P<second>[0-9]{2}))?)?"
    r"\s*(?P<ampm>am|pm)\b",
    re.IGNORECASE,
)


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        retryable: bool = True,
        diagnostics: Optional[Any] = None,
        cooldown_seconds: Optional[int] = None,
        provider_status: Optional[str] = None,
    ):
        if diagnostics is not None and hasattr(diagnostics, "summary"):
            message = "{}; {}".format(message, diagnostics.summary())
        super().__init__(message)
        self.retryable = retryable
        self.diagnostics = diagnostics
        self.cooldown_seconds = cooldown_seconds
        self.provider_status = provider_status


class ProviderSuperseded(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    stdout: str
    stderr: str
    final_message: str
    diagnostics: Optional[Any] = None
    usage: Optional[Dict[str, Any]] = None


def terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        proc.wait()


def run_selection_command(
    provider: str,
    cmd: list,
    prompt_file: Path,
    run_dir: str,
    env: dict,
    timeout_seconds: int,
    is_superseded: Callable[[], bool],
    output_file: Optional[Path] = None,
) -> str:
    stdout_file = Path(run_dir) / "{}-selection-stdout.log".format(provider)
    stderr_file = Path(run_dir) / "{}-selection-stderr.log".format(provider)
    timed_out = False
    with prompt_file.open("r", encoding="utf-8") as prompt_input, \
        stdout_file.open("w", encoding="utf-8") as stdout, \
        stderr_file.open("w", encoding="utf-8") as stderr:
        try:
            proc = subprocess.Popen(
                cmd, stdin=prompt_input, stdout=stdout, stderr=stderr,
                cwd=run_dir, text=True, env=env, start_new_session=True,
            )
        except OSError as exc:
            raise ProviderError("{} selection could not start: {}".format(provider, exc)) from exc
        started = time.monotonic()
        try:
            while proc.poll() is None:
                if is_superseded():
                    raise ProviderSuperseded("finding selection superseded")
                if time.monotonic() - started > timeout_seconds:
                    timed_out = True
                    break
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                terminate_process_group(proc)

    stdout_text = read_text(stdout_file)
    stderr_text = read_text(stderr_file)
    final_message = read_text(output_file) if output_file is not None else stdout_text
    cooldown_seconds = provider_quota_cooldown_seconds(provider, stdout_text, stderr_text, final_message)
    error = None
    if timed_out:
        error = "{} selection timed out after {} seconds".format(provider, timeout_seconds)
    elif proc.returncode != 0:
        error = "{} selection exited with status {}: {}".format(provider, proc.returncode, stderr_text[:1000])
    elif not final_message.strip():
        error = "{} selection did not write a final message".format(provider)
    if error:
        raise ProviderError(
            error, cooldown_seconds=cooldown_seconds,
            provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
        )
    return final_message


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def redacted_cmd(cmd: list) -> list:
    redacted = []
    for part in cmd:
        if isinstance(part, str) and len(part) > 200:
            redacted.append(part[:200] + "...<truncated>")
        else:
            redacted.append(part)
    return redacted


def _parse_reset_time_to_cooldown_seconds(
    normalized: str,
    now: Optional[datetime] = None,
) -> Optional[int]:
    match = _USAGE_LIMIT_RESET_TIME_RE.search(normalized)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or "0")
    second = int(match.group("second") or "0")
    if not 1 <= hour <= 12 or minute > 59 or second > 59:
        return None
    if match.group("ampm").lower() == "pm" and hour != 12:
        hour += 12
    elif match.group("ampm").lower() == "am" and hour == 12:
        hour = 0

    # Codex prints reset times in the invoking user's local clock.
    now_dt = now if now is not None else datetime.now().astimezone()
    target = now_dt.replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )
    if target <= now_dt:
        target += timedelta(days=1)
    return int((target - now_dt).total_seconds())


def provider_quota_cooldown_seconds(
    provider: str,
    *texts: str,
    now: Optional[datetime] = None,
) -> Optional[int]:
    if provider not in _COOLDOWN_PROVIDERS:
        return None
    normalized = re.sub(r"\s+", " ", "\n".join(text for text in texts if text).lower())
    five_hour_limit = bool(re.search(r"(?:5|five)[-\s]*hour(?:s)?", normalized)) and "limit" in normalized
    if "usage limit" not in normalized and not five_hour_limit:
        return None
    parsed_reset_seconds = _parse_reset_time_to_cooldown_seconds(normalized, now=now)
    if parsed_reset_seconds is not None:
        return parsed_reset_seconds
    if any(phrase in normalized for phrase in _USAGE_LIMIT_PHRASES):
        return DEFAULT_PROVIDER_COOLDOWN_SECONDS
    if any(context in normalized for context in _USAGE_LIMIT_CONTEXT) and any(
        marker in normalized for marker in ("reached", "hit", "exceeded")
    ):
        return DEFAULT_PROVIDER_COOLDOWN_SECONDS
    if five_hour_limit:
        return DEFAULT_PROVIDER_COOLDOWN_SECONDS
    return None
