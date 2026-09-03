from __future__ import annotations

from typing import Dict, List

from .review_plan import ReviewPlan, format_review_plan


def build_provider_prompt(provider: str, context: Dict[str, object], schema_path: str, review_plan: ReviewPlan) -> str:
    if provider == "codex":
        return build_codex_prompt(context, schema_path, review_plan)
    if provider == "claude":
        return build_claude_prompt(context, schema_path, review_plan)
    raise ValueError("unsupported provider: {}".format(provider))


def build_codex_prompt(context: Dict[str, object], schema_path: str, review_plan: ReviewPlan) -> str:
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


def build_claude_prompt(context: Dict[str, object], schema_path: str, review_plan: ReviewPlan) -> str:
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
    context: Dict[str, object],
    schema_path: str,
    review_plan: ReviewPlan,
) -> str:
    formatted_context = dict(context)
    formatted_context["related_repositories"] = _format_related_repositories(
        context.get("related_repositories", [])
    )
    formatted_context["compatibility_guidance"] = _format_compatibility_guidance(
        context.get("related_repositories", [])
    )
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

Related repositories (supporting context only):
{related_repositories}

Review only committed changes in the PR diff from merge base to HEAD. You may
inspect surrounding repository code and the directly listed related repositories
when needed to understand contracts, callers, schemas, or shared behavior. Do
not recursively look for other repositories. Every finding must refer to the
primary repository and be anchored to a changed line listed in the primary PR
diff. Never report findings against related-repository files. Do not modify
files. Do not perform network operations. Do not invent line numbers.

Compatibility lens:
{compatibility_guidance}

{review_plan_text}

{subagent_instructions}

Keep the reviewer outputs separate until every listed subagent has completed.
Deduplicate overlapping findings, preserve the contributing reviewer lens in
each final annotation, and drop weak or style-only findings unless they create
correctness, security, test, performance, maintainability, or compatibility
risk. Do not stop
after finding one issue; continue until every changed file has been considered
by the relevant reviewer lenses.

smallest_fix must remain prose that explains the smallest safe correction. Add
suggested_change.replacement only when it is the exact single-line replacement
for the annotated line. Set suggested_change to null for multi-line fixes,
uncertain fixes, conceptual guidance, or fixes that require surrounding edits.

Return exactly one schema-shaped JSON object, and only as the final answer.
Do not emit progress, status, or placeholder JSON. recommendation must be
approve when there are no material findings, or request_changes when there are
actionable findings.
""".format(
        intro=intro,
        schema_path=schema_path,
        subagent_instructions=subagent_instructions,
        review_plan_text=format_review_plan(review_plan),
        **formatted_context
    )


def _format_related_repositories(value: object) -> str:
    if not value:
        return "- None configured."
    repositories: List[Dict[str, str]] = value  # type: ignore[assignment]
    return "\n".join(
        "- {slug}: ref={ref}, commit={commit}, path={path}".format(**repo)
        for repo in repositories
    )


def _format_compatibility_guidance(related_repositories: object) -> str:
    base = (
        "- Compare changed externally consumed interfaces, configuration, and data or wire "
        "formats with compatibility evidence visible in the repositories."
    )
    if not related_repositories:
        return (
            "{}\n- No related repositories are configured. Use only contracts visible in the "
            "primary repository; do not infer other components or force a compatibility finding."
        ).format(
            base,
        )
    return """{}
- Use the listed related revisions to identify cross-repository contract, version-skew, and rollout-order risks caused by changed primary-repository lines.
- Ground compatibility findings in visible interfaces, compatibility shims, versioning or deprecation policy, tests, documentation, or other repository evidence.
- When that evidence describes supported older or newer related-component versions, check their interoperability with the primary change.
- Do not assert behavior for unavailable versions. Each related checkout proves only the listed revision, not every deployed version combination.""".format(
        base
    )
