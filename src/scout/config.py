from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9
    import tomli as tomllib  # type: ignore


class ConfigError(ValueError):
    pass


REVIEW_LENS_COUNT = 5
SUPPORTED_PROVIDERS = ("codex", "claude")
COMMENT_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
REVIEW_OUTPUT_MODES = ("reports", "inline_comments")


@dataclass(frozen=True)
class RepositoryConfig:
    slug: str
    clone_url: str
    pr_ids: List[int]
    ignored_source_branches: List[str]
    ignored_target_branches: List[str]
    ignore_draft_pull_requests: bool


@dataclass(frozen=True)
class ServiceConfig:
    worker_id: str
    state_db: str
    state_dir: str
    log_level: str
    retention_days: int


@dataclass(frozen=True)
class BitbucketConfig:
    workspace: str
    api_base_url: str
    api_auth: str
    api_username_credential: str
    api_key_credential: str
    ssh_key_credential: str
    repositories: List[RepositoryConfig]


@dataclass(frozen=True)
class PollingConfig:
    enabled: bool
    interval_seconds: int


@dataclass(frozen=True)
class QueueConfig:
    max_parallel_reviews: int
    job_timeout_seconds: int
    max_attempts: int
    retry_backoff_seconds: int


@dataclass(frozen=True)
class ReviewConfig:
    policy_version: str
    schema_path: str
    max_findings: int
    output_mode: str
    risk: "RiskConfig"
    request_comments: "RequestCommentsConfig"
    subagent_small_loc_limit: int
    subagent_medium_loc_limit: int
    subagent_large_loc_limit: int
    subagent_high_risk_bonus: int
    subagent_max_per_lens: int


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool
    provider: str
    model: str
    effort: str
    timeout_seconds: int


@dataclass(frozen=True)
class RequestCommentsConfig:
    provider: str
    model: str
    effort: str
    timeout_seconds: int


@dataclass(frozen=True)
class CodexConfig:
    enabled: bool
    auth_mode: str
    credential: str
    home_dir: str
    max_parallel: int
    timeout_seconds: int
    command: str
    model: str
    reasoning_effort: str
    fast_mode: bool
    max_subagents: int
    subagent_max_per_lens: int
    subagent_small_loc_limit: int = 150
    subagent_medium_loc_limit: int = 600
    subagent_large_loc_limit: int = 1500
    subagent_high_risk_bonus: int = 1


@dataclass(frozen=True)
class ClaudeConfig:
    enabled: bool
    auth_mode: str
    credential: str
    home_dir: str
    max_parallel: int
    timeout_seconds: int
    command: str
    model: str
    effort: str
    max_subagents: int
    subagent_max_per_lens: int
    subagent_small_loc_limit: int = 150
    subagent_medium_loc_limit: int = 600
    subagent_large_loc_limit: int = 1500
    subagent_high_risk_bonus: int = 1


@dataclass(frozen=True)
class AgentsConfig:
    strategy: str
    providers: List[str]
    codex: CodexConfig
    claude: ClaudeConfig


@dataclass(frozen=True)
class ReportsConfig:
    report_id: str
    title: str
    report_ids: Dict[str, str]
    titles: Dict[str, str]

    def report_id_for(self, provider: str) -> str:
        try:
            return self.report_ids[provider]
        except KeyError:
            raise ConfigError("missing report_id for provider {}".format(provider))

    def title_for(self, provider: str) -> str:
        try:
            return self.titles[provider]
        except KeyError:
            raise ConfigError("missing report title for provider {}".format(provider))


@dataclass(frozen=True)
class CommentsConfig:
    critical_enabled: bool
    severities: List[str]


@dataclass(frozen=True)
class AppConfig:
    service: ServiceConfig
    bitbucket: BitbucketConfig
    polling: PollingConfig
    queue: QueueConfig
    review: ReviewConfig
    agents: AgentsConfig
    reports: ReportsConfig
    comments: CommentsConfig


def load_config(path: str) -> AppConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return parse_config(raw)


def parse_config(raw: Dict[str, Any]) -> AppConfig:
    service = raw.get("service", {})
    bitbucket = raw.get("bitbucket", {})
    polling = raw.get("polling", {})
    queue = raw.get("queue", {})
    review = raw.get("review", {})
    agents = raw.get("agents", {})
    codex = agents.get("codex", {})
    claude = agents.get("claude", {})
    reports = raw.get("reports", {})
    comments = raw.get("comments", {})

    repositories = [
        RepositoryConfig(
            slug=_required_str(repo, "slug", "bitbucket.repositories"),
            clone_url=_required_str(repo, "clone_url", "bitbucket.repositories"),
            pr_ids=_int_list(repo.get("pr_ids", []), "bitbucket.repositories.pr_ids"),
            ignored_source_branches=_regex_list(
                repo.get("ignored_source_branches", []),
                "bitbucket.repositories.ignored_source_branches",
            ),
            ignored_target_branches=_regex_list(
                repo.get("ignored_target_branches", []),
                "bitbucket.repositories.ignored_target_branches",
            ),
            ignore_draft_pull_requests=_bool_value(
                repo.get("ignore_draft_pull_requests", False),
                "bitbucket.repositories.ignore_draft_pull_requests",
            ),
        )
        for repo in bitbucket.get("repositories", [])
    ]
    if not repositories:
        raise ConfigError("bitbucket.repositories must contain at least one repository")

    api_auth = str(bitbucket.get("api_auth", "basic"))
    if api_auth != "basic":
        raise ConfigError("only bitbucket.api_auth = \"basic\" is supported in v1")

    codex_auth_mode = str(codex.get("auth_mode", "logged_in"))
    if codex_auth_mode not in {"logged_in", "api"}:
        raise ConfigError("agents.codex.auth_mode must be logged_in or api")
    codex_reasoning_effort = str(codex.get("reasoning_effort", "xhigh"))
    if codex_reasoning_effort not in {"low", "medium", "high", "xhigh"}:
        raise ConfigError("agents.codex.reasoning_effort must be low, medium, high, or xhigh")

    claude_auth_mode = str(claude.get("auth_mode", "logged_in"))
    if claude_auth_mode not in {"logged_in", "api"}:
        raise ConfigError("agents.claude.auth_mode must be logged_in or api")
    claude_effort = str(claude.get("effort", "max"))
    if claude_effort and claude_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ConfigError("agents.claude.effort must be empty or one of low, medium, high, xhigh, or max")

    if "providers" in agents:
        selected_providers = _provider_list(agents.get("providers"), "agents.providers")
        strategy = str(agents.get("strategy", selected_providers[0]))
        if strategy not in SUPPORTED_PROVIDERS:
            raise ConfigError("agents.strategy must be codex or claude")
        if strategy not in selected_providers:
            raise ConfigError("agents.strategy must be included in agents.providers")
    else:
        strategy = str(agents.get("strategy", "codex"))
        if strategy not in SUPPORTED_PROVIDERS:
            raise ConfigError("agents.strategy must be codex or claude")
        selected_providers = [strategy]
    provider_configs = {"codex": codex, "claude": claude}
    for provider in selected_providers:
        if not _provider_enabled(provider, provider_configs[provider]):
            raise ConfigError("agents.providers must only include enabled agent providers")

    output_mode = str(review.get("output_mode", "reports"))
    if output_mode not in REVIEW_OUTPUT_MODES:
        raise ConfigError("review.output_mode must be reports or inline_comments")
    risk_config = _parse_risk_config(review, codex, claude)
    request_comments_config = _parse_request_comments_config(review, codex, claude, output_mode)

    state_dir = str(service.get("state_dir", "/var/lib/scout"))
    retention_days = _positive_int(service.get("retention_days", 7), "service.retention_days")
    if retention_days > 7:
        raise ConfigError("service.retention_days must be 7 or less")
    review_subagent_small_loc_limit = _positive_int(
        review.get("subagent_small_loc_limit", 150),
        "review.subagent_small_loc_limit",
    )
    review_subagent_medium_loc_limit = _positive_int(
        review.get("subagent_medium_loc_limit", 600),
        "review.subagent_medium_loc_limit",
    )
    review_subagent_large_loc_limit = _positive_int(
        review.get("subagent_large_loc_limit", 1500),
        "review.subagent_large_loc_limit",
    )
    _validate_subagent_loc_limits(
        review_subagent_small_loc_limit,
        review_subagent_medium_loc_limit,
        review_subagent_large_loc_limit,
        "review",
    )
    review_subagent_high_risk_bonus = _non_negative_int(
        review.get("subagent_high_risk_bonus", 1),
        "review.subagent_high_risk_bonus",
    )
    review_subagent_max_per_lens = _positive_int(
        review.get("subagent_max_per_lens", 4),
        "review.subagent_max_per_lens",
    )
    review_sizing = {
        "subagent_small_loc_limit": review_subagent_small_loc_limit,
        "subagent_medium_loc_limit": review_subagent_medium_loc_limit,
        "subagent_large_loc_limit": review_subagent_large_loc_limit,
        "subagent_high_risk_bonus": review_subagent_high_risk_bonus,
    }
    codex_review_sizing = _parse_provider_review_sizing("codex", codex, review_sizing)
    claude_review_sizing = _parse_provider_review_sizing("claude", claude, review_sizing)
    codex_max_subagents = _positive_int(codex.get("max_subagents", 15), "agents.codex.max_subagents")
    claude_max_subagents = _positive_int(claude.get("max_subagents", 20), "agents.claude.max_subagents")
    codex_subagent_max_per_lens = _positive_int(
        codex.get("subagent_max_per_lens", min(review_subagent_max_per_lens, 3)),
        "agents.codex.subagent_max_per_lens",
    )
    codex_subagent_max_per_lens_label = (
        "agents.codex.subagent_max_per_lens"
        if "subagent_max_per_lens" in codex
        else "agents.codex.subagent_max_per_lens"
    )
    claude_subagent_max_per_lens = _positive_int(
        claude.get("subagent_max_per_lens", 1),
        "agents.claude.subagent_max_per_lens",
    )
    claude_subagent_max_per_lens_label = (
        "agents.claude.subagent_max_per_lens"
        if "subagent_max_per_lens" in claude
        else "agents.claude.subagent_max_per_lens"
    )
    provider_max_subagents = {
        "codex": codex_max_subagents,
        "claude": claude_max_subagents,
    }
    provider_subagent_max_per_lens = {
        "codex": codex_subagent_max_per_lens,
        "claude": claude_subagent_max_per_lens,
    }
    provider_subagent_max_per_lens_label = {
        "codex": codex_subagent_max_per_lens_label,
        "claude": claude_subagent_max_per_lens_label,
    }
    for provider in selected_providers:
        selected_max_subagents = provider_max_subagents[provider]
        selected_subagent_max_per_lens = provider_subagent_max_per_lens[provider]
        selected_subagent_max_per_lens_label = provider_subagent_max_per_lens_label[provider]
        if selected_subagent_max_per_lens * REVIEW_LENS_COUNT > selected_max_subagents:
            raise ConfigError(
                "{} permits {} total subagents, "
                "which exceeds agents.{}.max_subagents={}".format(
                    selected_subagent_max_per_lens_label,
                    selected_subagent_max_per_lens * REVIEW_LENS_COUNT,
                    provider,
                    selected_max_subagents,
                )
            )
    report_ids, report_titles = _parse_report_overrides(reports, selected_providers)
    comment_severities = [] if output_mode == "inline_comments" else _comment_severity_list(comments)

    return AppConfig(
        service=ServiceConfig(
            worker_id=str(service.get("worker_id", "reviewer-1")),
            state_db=str(service.get("state_db", os.path.join(state_dir, "state.db"))),
            state_dir=state_dir,
            log_level=str(service.get("log_level", "INFO")),
            retention_days=retention_days,
        ),
        bitbucket=BitbucketConfig(
            workspace=_required_str(bitbucket, "workspace", "bitbucket"),
            api_base_url=str(bitbucket.get("api_base_url", "https://api.bitbucket.org/2.0")).rstrip("/"),
            api_auth=api_auth,
            api_username_credential=str(bitbucket.get("api_username_credential", "bitbucket_username")),
            api_key_credential=str(bitbucket.get("api_key_credential", "bitbucket_api_key")),
            ssh_key_credential=str(bitbucket.get("ssh_key_credential", "bitbucket_ssh_key")),
            repositories=repositories,
        ),
        polling=PollingConfig(
            enabled=bool(polling.get("enabled", True)),
            interval_seconds=_positive_int(polling.get("interval_seconds", 600), "polling.interval_seconds"),
        ),
        queue=QueueConfig(
            max_parallel_reviews=_positive_int(queue.get("max_parallel_reviews", 2), "queue.max_parallel_reviews"),
            job_timeout_seconds=_positive_int(queue.get("job_timeout_seconds", 1800), "queue.job_timeout_seconds"),
            max_attempts=_positive_int(queue.get("max_attempts", 3), "queue.max_attempts"),
            retry_backoff_seconds=_positive_int(
                queue.get("retry_backoff_seconds", 300),
                "queue.retry_backoff_seconds",
            ),
        ),
        review=ReviewConfig(
            policy_version=str(review.get("policy_version", "v1")),
            schema_path=str(review.get("schema_path", "/etc/scout/review.schema.json")),
            max_findings=_positive_int(review.get("max_findings", 100), "review.max_findings"),
            output_mode=output_mode,
            risk=risk_config,
            request_comments=request_comments_config,
            subagent_small_loc_limit=review_subagent_small_loc_limit,
            subagent_medium_loc_limit=review_subagent_medium_loc_limit,
            subagent_large_loc_limit=review_subagent_large_loc_limit,
            subagent_high_risk_bonus=review_subagent_high_risk_bonus,
            subagent_max_per_lens=review_subagent_max_per_lens,
        ),
        agents=AgentsConfig(
            strategy=strategy,
            providers=selected_providers,
            codex=CodexConfig(
                enabled=bool(codex.get("enabled", True)),
                auth_mode=codex_auth_mode,
                credential=str(codex.get("credential", "codex")),
                home_dir=str(codex.get("home_dir", "/var/lib/scout/agents/codex/main")),
                max_parallel=_positive_int(codex.get("max_parallel", 2), "agents.codex.max_parallel"),
                timeout_seconds=_positive_int(codex.get("timeout_seconds", 1800), "agents.codex.timeout_seconds"),
                command=str(codex.get("command", "codex")),
                model=str(codex.get("model", "gpt-5.5")),
                reasoning_effort=codex_reasoning_effort,
                fast_mode=bool(codex.get("fast_mode", True)),
                max_subagents=codex_max_subagents,
                subagent_max_per_lens=codex_subagent_max_per_lens,
                subagent_small_loc_limit=codex_review_sizing["subagent_small_loc_limit"],
                subagent_medium_loc_limit=codex_review_sizing["subagent_medium_loc_limit"],
                subagent_large_loc_limit=codex_review_sizing["subagent_large_loc_limit"],
                subagent_high_risk_bonus=codex_review_sizing["subagent_high_risk_bonus"],
            ),
            claude=ClaudeConfig(
                enabled=_provider_enabled("claude", claude),
                auth_mode=claude_auth_mode,
                credential=str(claude.get("credential", "claude")),
                home_dir=str(claude.get("home_dir", "/var/lib/scout/agents/claude/main")),
                max_parallel=_positive_int(claude.get("max_parallel", 2), "agents.claude.max_parallel"),
                timeout_seconds=_positive_int(
                    claude.get("timeout_seconds", 1800),
                    "agents.claude.timeout_seconds",
                ),
                command=str(claude.get("command", "claude")),
                model=str(claude.get("model", "claude-sonnet-4-6")),
                effort=claude_effort,
                max_subagents=claude_max_subagents,
                subagent_max_per_lens=claude_subagent_max_per_lens,
                subagent_small_loc_limit=claude_review_sizing["subagent_small_loc_limit"],
                subagent_medium_loc_limit=claude_review_sizing["subagent_medium_loc_limit"],
                subagent_large_loc_limit=claude_review_sizing["subagent_large_loc_limit"],
                subagent_high_risk_bonus=claude_review_sizing["subagent_high_risk_bonus"],
            ),
        ),
        reports=ReportsConfig(
            report_id=report_ids[strategy],
            title=report_titles[strategy],
            report_ids=report_ids,
            titles=report_titles,
        ),
        comments=CommentsConfig(
            critical_enabled=bool(comments.get("critical_enabled", True)),
            severities=comment_severities,
        ),
    )


class CredentialStore:
    def __init__(self, credentials_dir: Optional[str] = None):
        self.credentials_dir = credentials_dir or os.environ.get("CREDENTIALS_DIRECTORY")

    def path(self, name: str) -> Path:
        if not self.credentials_dir:
            raise ConfigError("CREDENTIALS_DIRECTORY is not set")
        path = Path(self.credentials_dir) / name
        if not path.is_file():
            raise ConfigError("missing systemd credential: {}".format(name))
        return path

    def read(self, name: str) -> str:
        return self.path(name).read_text(encoding="utf-8").strip()


def _required_str(section: Dict[str, Any], key: str, label: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError("{}.{} is required".format(label, key))
    return value


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ConfigError("{} must be a positive integer".format(label))
    if parsed <= 0:
        raise ConfigError("{} must be a positive integer".format(label))
    return parsed


def _non_negative_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ConfigError("{} must be a non-negative integer".format(label))
    if parsed < 0:
        raise ConfigError("{} must be a non-negative integer".format(label))
    return parsed


def _bool_value(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("{} must be a boolean".format(label))
    return value


def _validate_subagent_loc_limits(
    small_loc_limit: int,
    medium_loc_limit: int,
    large_loc_limit: int,
    label: str,
) -> None:
    if not (small_loc_limit < medium_loc_limit < large_loc_limit):
        raise ConfigError(
            "{} subagent LOC limits must be strictly increasing: "
            "subagent_small_loc_limit < subagent_medium_loc_limit < subagent_large_loc_limit".format(label)
        )


def _parse_provider_review_sizing(
    provider_name: str,
    provider_config: Dict[str, Any],
    defaults: Dict[str, int],
) -> Dict[str, int]:
    small_loc_limit = _positive_int(
        provider_config.get("subagent_small_loc_limit", defaults["subagent_small_loc_limit"]),
        "agents.{}.subagent_small_loc_limit".format(provider_name),
    )
    medium_loc_limit = _positive_int(
        provider_config.get("subagent_medium_loc_limit", defaults["subagent_medium_loc_limit"]),
        "agents.{}.subagent_medium_loc_limit".format(provider_name),
    )
    large_loc_limit = _positive_int(
        provider_config.get("subagent_large_loc_limit", defaults["subagent_large_loc_limit"]),
        "agents.{}.subagent_large_loc_limit".format(provider_name),
    )
    _validate_subagent_loc_limits(
        small_loc_limit,
        medium_loc_limit,
        large_loc_limit,
        "agents.{}".format(provider_name),
    )
    high_risk_bonus = _non_negative_int(
        provider_config.get("subagent_high_risk_bonus", defaults["subagent_high_risk_bonus"]),
        "agents.{}.subagent_high_risk_bonus".format(provider_name),
    )
    return {
        "subagent_small_loc_limit": small_loc_limit,
        "subagent_medium_loc_limit": medium_loc_limit,
        "subagent_large_loc_limit": large_loc_limit,
        "subagent_high_risk_bonus": high_risk_bonus,
    }


def _parse_risk_config(
    review: Dict[str, Any],
    codex: Dict[str, Any],
    claude: Dict[str, Any],
) -> RiskConfig:
    risk = review.get("risk", {})
    if risk is None:
        risk = {}
    if not isinstance(risk, dict):
        raise ConfigError("review.risk must be a table")

    enabled = _bool_value(risk.get("enabled", True), "review.risk.enabled")
    provider = str(risk.get("provider", "codex"))
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError("review.risk.provider must be codex or claude")
    provider_agent_config = {"codex": codex, "claude": claude}[provider]
    if enabled and not _provider_enabled(provider, provider_agent_config):
        raise ConfigError("review.risk.provider must name an enabled agent provider")

    if "codex" in risk or "claude" in risk:
        raise ConfigError("review.risk uses provider-agnostic model and effort keys")
    model = str(risk.get("model", "claude-sonnet-4-6" if provider == "claude" else "gpt-5.4"))
    effort = str(risk.get("effort", "low"))
    if provider == "codex" and effort not in {"low", "medium", "high", "xhigh"}:
        raise ConfigError("review.risk.effort must be low, medium, high, or xhigh for codex")
    if provider == "claude" and effort and effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ConfigError("review.risk.effort must be empty or one of low, medium, high, xhigh, or max for claude")

    return RiskConfig(
        enabled=enabled,
        provider=provider,
        model=model,
        effort=effort,
        timeout_seconds=_positive_int(risk.get("timeout_seconds", 120), "review.risk.timeout_seconds"),
    )


def _parse_request_comments_config(
    review: Dict[str, Any],
    codex: Dict[str, Any],
    claude: Dict[str, Any],
    output_mode: str,
) -> RequestCommentsConfig:
    request_comments = review.get("request_comments", {})
    if request_comments is None:
        request_comments = {}
    if not isinstance(request_comments, dict):
        raise ConfigError("review.request_comments must be a table")

    provider = str(request_comments.get("provider", "codex"))
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError("review.request_comments.provider must be codex or claude")
    provider_agent_config = {"codex": codex, "claude": claude}[provider]
    if output_mode == "inline_comments" and not _provider_enabled(provider, provider_agent_config):
        raise ConfigError("review.request_comments.provider must name an enabled agent provider")

    if "codex" in request_comments or "claude" in request_comments:
        raise ConfigError("review.request_comments uses provider-agnostic model and effort keys")
    model = str(request_comments.get("model", "claude-sonnet-4-6" if provider == "claude" else "gpt-5.4"))
    effort = str(request_comments.get("effort", "low"))
    if provider == "codex" and effort not in {"low", "medium", "high", "xhigh"}:
        raise ConfigError("review.request_comments.effort must be low, medium, high, or xhigh for codex")
    if provider == "claude" and effort and effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ConfigError(
            "review.request_comments.effort must be empty or one of low, medium, high, xhigh, or max for claude"
        )

    return RequestCommentsConfig(
        provider=provider,
        model=model,
        effort=effort,
        timeout_seconds=_positive_int(
            request_comments.get("timeout_seconds", 120),
            "review.request_comments.timeout_seconds",
        ),
    )


def _provider_enabled(provider: str, provider_config: Dict[str, Any]) -> bool:
    default_enabled = provider != "claude"
    return _bool_value(provider_config.get("enabled", default_enabled), "agents.{}.enabled".format(provider))


def _int_list(value: Any, label: str) -> List[int]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ConfigError("{} must be a list of positive integers".format(label))
    parsed = []
    for item in value:
        parsed.append(_positive_int(item, label))
    return parsed


def _regex_list(value: Any, label: str) -> List[str]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ConfigError("{} must be a list of regex patterns".format(label))
    patterns = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError("{} must be a list of regex patterns".format(label))
        try:
            re.compile(item)
        except re.error as exc:
            raise ConfigError("{} contains invalid regex pattern {}: {}".format(label, item, exc))
        patterns.append(item)
    return patterns


def _provider_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError("{} must be a non-empty list containing codex and/or claude".format(label))
    providers = []
    seen = set()
    for item in value:
        provider = str(item)
        if provider not in SUPPORTED_PROVIDERS:
            raise ConfigError("{} contains unsupported provider: {}".format(label, provider))
        if provider in seen:
            raise ConfigError("{} contains duplicate provider: {}".format(label, provider))
        providers.append(provider)
        seen.add(provider)
    return providers


def _comment_severity_list(comments: Dict[str, Any]) -> List[str]:
    if "severities" in comments:
        value = comments["severities"]
    elif "comment_severities" in comments:
        value = comments["comment_severities"]
    elif bool(comments.get("critical_enabled", True)):
        return ["CRITICAL"]
    else:
        return []

    if not isinstance(value, list):
        raise ConfigError("comments.severities must be a list")
    severities: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ConfigError("comments.severities entries must be strings")
        severity = item.upper()
        if severity not in COMMENT_SEVERITIES:
            raise ConfigError(
                "comments.severities entries must be one of {}".format(
                    ", ".join(COMMENT_SEVERITIES)
                )
            )
        if severity not in seen:
            severities.append(severity)
            seen.add(severity)
    return severities


def _parse_report_overrides(
    reports: Dict[str, Any],
    selected_providers: List[str],
) -> tuple:
    if len(selected_providers) > 1 and ("report_id" in reports or "title" in reports):
        raise ConfigError(
            "reports.report_id and reports.title are only valid for single-provider configs; "
            "use reports.<provider>.report_id and reports.<provider>.title"
        )
    report_ids: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    single_provider = len(selected_providers) == 1
    for provider in selected_providers:
        provider_reports = reports.get(provider, {})
        if provider_reports is None:
            provider_reports = {}
        if not isinstance(provider_reports, dict):
            raise ConfigError("reports.{} must be a table".format(provider))
        if single_provider and "report_id" in reports:
            report_id = str(reports["report_id"])
        else:
            report_id = str(provider_reports.get("report_id", "scout-{}-v1".format(provider)))
        if single_provider and "title" in reports:
            title = str(reports["title"])
        else:
            title = str(provider_reports.get("title", "{} PR Review".format(_provider_label(provider))))
        report_ids[provider] = report_id
        titles[provider] = title
    if len(selected_providers) > 1 and len(set(report_ids.values())) != len(report_ids):
        raise ConfigError("reports.<provider>.report_id values must be unique")
    return report_ids, titles


def _provider_label(provider: str) -> str:
    labels = {
        "codex": "Codex",
        "claude": "Claude",
    }
    return labels.get(provider, provider.replace("_", " ").replace("-", " ").title())
