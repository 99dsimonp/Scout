from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


CODEX_TOTAL_TOKENS_RE = re.compile(
    r"(?:^|\n)\s*tokens used\s*\n\s*(?P<tokens>[0-9][0-9,]*)\b",
    re.IGNORECASE,
)


def parse_claude_usage(stdout_text: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    model_usage = payload.get("modelUsage")
    models: Dict[str, Dict[str, Any]] = {}
    if isinstance(model_usage, dict):
        for model, values in model_usage.items():
            if not isinstance(model, str) or not isinstance(values, dict):
                continue
            normalized = {
                "input_tokens": _int_value(values.get("inputTokens")),
                "output_tokens": _int_value(values.get("outputTokens")),
                "cache_creation_input_tokens": _int_value(values.get("cacheCreationInputTokens")),
                "cache_read_input_tokens": _int_value(values.get("cacheReadInputTokens")),
                "cost_usd": _float_value(values.get("costUSD")),
            }
            normalized["total_tokens"] = _token_total(normalized)
            models[model] = normalized

    usage = payload.get("usage")
    top_level: Dict[str, Any] = {}
    if isinstance(usage, dict):
        top_level = {
            "input_tokens": _int_value(usage.get("input_tokens")),
            "output_tokens": _int_value(usage.get("output_tokens")),
            "cache_creation_input_tokens": _int_value(usage.get("cache_creation_input_tokens")),
            "cache_read_input_tokens": _int_value(usage.get("cache_read_input_tokens")),
        }
        top_level["total_tokens"] = _token_total(top_level)

    if models:
        aggregate = {
            "input_tokens": sum(model["input_tokens"] for model in models.values()),
            "output_tokens": sum(model["output_tokens"] for model in models.values()),
            "cache_creation_input_tokens": sum(
                model["cache_creation_input_tokens"] for model in models.values()
            ),
            "cache_read_input_tokens": sum(model["cache_read_input_tokens"] for model in models.values()),
            "cost_usd": sum(model["cost_usd"] for model in models.values()),
            "models": models,
        }
        aggregate["total_tokens"] = _token_total(aggregate)
    elif top_level:
        aggregate = dict(top_level)
        aggregate["cost_usd"] = _float_value(payload.get("total_cost_usd"))
    else:
        return None

    if not aggregate.get("cost_usd"):
        aggregate["cost_usd"] = _float_value(payload.get("total_cost_usd"))
    aggregate["source"] = "claude_stdout_json"
    return aggregate


def parse_codex_usage(stdout_text: str, stderr_text: str, final_message: str = "") -> Optional[Dict[str, Any]]:
    combined = "\n".join(text for text in (stdout_text, stderr_text, final_message) if text)
    matches = list(CODEX_TOTAL_TOKENS_RE.finditer(combined))
    if not matches:
        return None
    total_tokens = int(matches[-1].group("tokens").replace(",", ""))
    return {
        "total_tokens": total_tokens,
        "source": "codex_tokens_used",
    }


def parse_provider_usage(
    provider: str,
    stdout_text: str,
    stderr_text: str,
    final_message: str = "",
) -> Optional[Dict[str, Any]]:
    if provider == "claude":
        return parse_claude_usage(stdout_text)
    if provider == "codex":
        return parse_codex_usage(stdout_text, stderr_text, final_message)
    return None


def parse_provider_usage_from_logs(provider: str, run_dir: str) -> Optional[Dict[str, Any]]:
    run_path = Path(run_dir)
    stdout_text = _read_text(run_path / "{}-stdout.log".format(provider))
    stderr_text = _read_text(run_path / "{}-stderr.log".format(provider))
    final_message = _read_text(run_path / "codex-final-message.json") if provider == "codex" else ""
    return parse_provider_usage(provider, stdout_text, stderr_text, final_message)


def summarize_usage_log(
    path: Path,
    repo: Optional[str] = None,
    pr: Optional[int] = None,
) -> List[Dict[str, Any]]:
    aggregates: Dict[tuple, Dict[str, Any]] = {}
    for entry in _read_jsonl(path):
        if repo is not None and entry.get("repo") != repo:
            continue
        if pr is not None and entry.get("pr") != pr:
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        key = (entry.get("workspace"), entry.get("repo"), entry.get("pr"), entry.get("provider"))
        row = aggregates.setdefault(
            key,
            {
                "workspace": entry.get("workspace"),
                "repo": entry.get("repo"),
                "pr": entry.get("pr"),
                "provider": entry.get("provider"),
                "runs": 0,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cost_usd": 0.0,
                "latest_commit": None,
                "latest_timestamp": None,
            },
        )
        row["runs"] += 1
        for field in (
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            row[field] += _int_value(usage.get(field))
        row["cost_usd"] += _float_value(usage.get("cost_usd"))
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str) and (
            row["latest_timestamp"] is None or timestamp > row["latest_timestamp"]
        ):
            row["latest_timestamp"] = timestamp
            row["latest_commit"] = entry.get("commit")
    return sorted(
        aggregates.values(),
        key=lambda row: (row["total_tokens"], row["cost_usd"], row["runs"]),
        reverse=True,
    )


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _token_total(values: Dict[str, Any]) -> int:
    return (
        _int_value(values.get("input_tokens"))
        + _int_value(values.get("output_tokens"))
        + _int_value(values.get("cache_creation_input_tokens"))
        + _int_value(values.get("cache_read_input_tokens"))
    )


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _float_value(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0
