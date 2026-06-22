from __future__ import annotations

from typing import Dict

from .review_plan import ReviewPlan, format_review_plan


def build_provider_prompt(provider: str, context: Dict[str, str], schema_path: str, review_plan: ReviewPlan) -> str:
    if provider == "codex":
        return build_codex_prompt(context, schema_path, review_plan)
    if provider == "claude":
        return build_claude_prompt(context, schema_path, review_plan)
    raise ValueError("unsupported provider: {}".format(provider))


def build_codex_prompt(context: Dict[str, str], schema_path: str, review_plan: ReviewPlan) -> str:
    return _build_prompt(
        intro="You are reviewing a Bitbucket Cloud pull request for Scout.",
        subagent_instructions="""When spawning subagents, keep the current Codex model and reasoning effort.
Do not override agent type, model, or reasoning effort for forked subagents.
Each listed subagent should inspect the full diff and relevant surrounding code
from its assigned lens. Ask each subagent to return all actionable findings it
can support, not only the first or highest severity finding.""",
        context=context,
        schema_path=schema_path,
        review_plan=review_plan,
    )


def build_claude_prompt(context: Dict[str, str], schema_path: str, review_plan: ReviewPlan) -> str:
    return _build_prompt(
        intro="You are Claude reviewing a Bitbucket Cloud pull request for Scout.",
        subagent_instructions="""When spawning subagents, keep the current Claude model and effort configuration.
Do not override agent type, model, or effort for forked subagents.
Each listed subagent should inspect the full diff and relevant surrounding code
from its assigned lens. Ask each subagent to return all actionable findings it
can support, not only the first or highest severity finding.""",
        context=context,
        schema_path=schema_path,
        review_plan=review_plan,
    )


def _build_prompt(
    intro: str,
    subagent_instructions: str,
    context: Dict[str, str],
    schema_path: str,
    review_plan: ReviewPlan,
) -> str:
    return """{intro}

Repository context:
- Workspace: {workspace}
- Repository: {repo_slug}
- PR ID: {pr_id}
- Title: {title}
- Description: {description}
- Source branch: {source_branch}
- Source commit: {source_commit}
- Target branch: {target_branch}
- Target commit: {target_commit}
- Merge base: {merge_base}
- Changed LOC: {changed_lines}

Generated review context files:
- Context JSON: {context_path}
- Changed files: {files_path}
- Diff patch: {diff_path}
- Output schema: {schema_path}

Review only committed changes in the PR diff from merge base to HEAD. You may
inspect surrounding repository code when needed to understand impact, but
findings must be anchored to changed lines. Do not modify files. Do not perform
network operations. Do not invent line numbers.

{review_plan_text}

{subagent_instructions}

Keep the reviewer outputs separate until every listed subagent has completed.
Deduplicate overlapping findings, preserve the contributing reviewer lens in
each final annotation, and drop weak or style-only findings unless they create
correctness, security, test, performance, or maintainability risk. Do not stop
after finding one issue; continue until every changed file has been considered
by the relevant reviewer lenses.

smallest_fix must remain prose that explains the smallest safe correction. Add
suggested_change.replacement only when it is the exact single-line replacement
for the annotated line. Omit suggested_change for multi-line fixes, uncertain
fixes, conceptual guidance, or fixes that require surrounding edits.

Return exactly one schema-shaped JSON object, and only as the final answer.
Do not emit progress, status, or placeholder JSON. recommendation must be
approve when there are no material findings, or request_changes when there are
actionable findings.
""".format(
        intro=intro,
        schema_path=schema_path,
        subagent_instructions=subagent_instructions,
        review_plan_text=format_review_plan(review_plan),
        **context
    )
