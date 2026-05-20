from __future__ import annotations

from dataclasses import dataclass
from typing import List


REVIEW_LENSES = ["correctness", "security", "tests", "performance", "best-practices"]
RISK_LEVELS = ("low", "medium", "high")
DEFAULT_RISK = "medium"


@dataclass(frozen=True)
class ReviewPlan:
    changed_lines: int
    high_risk: bool
    subagents_per_lens: int
    risk: str = DEFAULT_RISK

    @property
    def total_subagents(self) -> int:
        return self.subagents_per_lens * len(REVIEW_LENSES)

    @property
    def reviewers(self) -> List[str]:
        reviewers = []
        for lens in REVIEW_LENSES:
            if self.subagents_per_lens == 1:
                reviewers.append(lens)
            else:
                for index in range(1, self.subagents_per_lens + 1):
                    reviewers.append("{}-{}".format(lens, index))
        return reviewers


def build_review_plan(
    changed_lines: int,
    description: str,
    small_loc_limit: int,
    medium_loc_limit: int,
    large_loc_limit: int,
    high_risk_bonus: int,
    max_subagents_per_lens: int,
    risk: str = DEFAULT_RISK,
) -> ReviewPlan:
    risk_mode = normalize_risk(risk)
    if risk_mode == "low":
        subagents_per_lens = 1
    else:
        subagents_per_lens = _base_subagents_per_lens(
            changed_lines,
            small_loc_limit,
            medium_loc_limit,
            large_loc_limit,
        )
        if risk_mode == "high":
            subagents_per_lens += high_risk_bonus
    subagents_per_lens = min(subagents_per_lens, max_subagents_per_lens)
    return ReviewPlan(
        changed_lines=changed_lines,
        high_risk=risk_mode == "high",
        subagents_per_lens=subagents_per_lens,
        risk=risk_mode,
    )


def count_changed_lines(diff: str) -> int:
    changed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            changed += 1
    return changed


def normalize_risk(risk: str) -> str:
    normalized = str(risk or "").strip().lower()
    if normalized in RISK_LEVELS:
        return normalized
    return DEFAULT_RISK


def format_review_plan(plan: ReviewPlan) -> str:
    lines = [
        "Review sizing:",
        "- Changed LOC: {}".format(plan.changed_lines),
        "- PR description risk: {}".format(normalize_risk(plan.risk)),
        "- Subagents per review category: {}".format(plan.subagents_per_lens),
        "- Total reviewer subagents: {}".format(plan.total_subagents),
        "",
        "Use this exact reviewer subagent plan:",
    ]
    lines.extend("- {}".format(reviewer) for reviewer in plan.reviewers)
    return "\n".join(lines)


def _base_subagents_per_lens(
    changed_lines: int,
    small_loc_limit: int,
    medium_loc_limit: int,
    large_loc_limit: int,
) -> int:
    if changed_lines <= small_loc_limit:
        return 1
    if changed_lines <= medium_loc_limit:
        return 2
    if changed_lines <= large_loc_limit:
        return 3
    return 4
