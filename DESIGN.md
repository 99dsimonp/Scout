# Scout Design

## Summary

Scout is a self-hosted Linux service that reviews Bitbucket Cloud pull requests
with agentic CLI tools. v1 supports selecting Codex, Claude, or both in one
daemon process. It polls configured repositories for open pull requests, checks out
changed PR commits locally, runs AI reviewers against readonly worktrees,
validates strict JSON output, and publishes the results back to Bitbucket Cloud
as provider-specific Code Insights reports and annotations.

The v1 target is a Rocky Linux 9 compatible RPM installed with `dnf` or `yum`.
Scout runs as a systemd service with no public HTTP endpoint. Webhook ingestion,
multi-node operation, provider credential pools, and account rotation are
deferred.

The project name is Scout. The main daemon should use Scout naming consistently:

- Binary: `scout`
- Service: `scout.service`
- Config directory: `/etc/scout/`
- State directory: `/var/lib/scout/`
- Report ID prefix: `scout`

## Goals

- Detect open Bitbucket Cloud pull requests for configured repositories.
- Determine which PR commits require review.
- Maintain local git mirrors and detached worktrees.
- Run selected provider CLIs in headless mode.
- Require structured JSON output and validate it before publishing.
- Publish deterministic Bitbucket Code Insights reports and annotations.
- Run as a local systemd service under a dedicated user.
- Package as an RPM for Rocky Linux 9 / Enterprise Linux 9 compatible systems.
- Keep secrets out of the main configuration file and out of agent environments
  unless specifically required by that provider.

## Non-Goals for v1

- Public webhook endpoint.
- Web UI or API server.
- Multi-node scheduling.
- Credential/account rotation.
- Quota bypassing through user account pools.
- Full hostile-repository sandboxing.
- Merged multi-provider consensus review.

The reviewed repositories and Bitbucket workspace are assumed to be trusted by
the service operator. Scout should still use readonly checkouts, minimal
subprocess environments, timeouts, and credential separation as operational
hygiene.

## Architecture

Scout v1 is a single Python daemon:

```text
Bitbucket Cloud
      |
      | poll open PRs
      v
scout
      |
      | state and queue
      v
SQLite
      |
      | checkout PR commit
      v
local git mirror + readonly worktree
      |
      | run selected provider CLI(s)
      v
Codex or Claude CLI
      |
      | strict JSON
      v
schema validation
      |
      | report + annotations
      v
Bitbucket Code Insights
```

The daemon owns all privileged responsibilities:

- Poll Bitbucket Cloud.
- Maintain SQLite state.
- Queue review jobs.
- Manage git mirrors and worktrees.
- Invoke provider CLIs.
- Validate agent output.
- Publish reports and annotations.
- Apply retries, cooldowns, and timeouts.

The daemon should not expose an HTTP listener in v1.

## Future Webhook Service

A future webhook receiver may be added as a separate service:

- Service: `scout-webhook.service`
- Receives Bitbucket webhook events.
- Verifies authenticity.
- Deduplicates events.
- Writes queue entries into the same state database or future queue backend.

The webhook service should not hold agent credentials, publish reports, or check
out repositories. Keeping it separate limits the blast radius of the
internet-facing component.

## Language and Runtime

Python 3.9 is the v1 implementation baseline. This matches Rocky Linux 9 and
keeps the first RPM target aligned with the oldest supported deployment OS.

The current implementation intentionally keeps runtime dependencies small:

- Standard-library `urllib.request` for Bitbucket API calls.
- Standard-library `sqlite3` for local state.
- Standard-library `concurrent.futures.ThreadPoolExecutor` for bounded worker
  concurrency.
- Standard-library `logging` for service logs.
- Manual schema validation in `scout.schema` plus provider-side schema flags
  where supported.
- `tomli` for TOML parsing on Python 3.9. Python 3.11+ uses `tomllib`.

Scout should prefer boring service behavior over framework complexity. A simple
daemon loop with explicit poll, lease, review, publish, and cleanup phases is
enough for v1.

## RPM and Linux Packaging

The v1 packaging baseline is native RPM packaging for Rocky Linux 9 /
Enterprise Linux 9 compatible systems.

Build recommendations:

- Build in an EL9 target environment, preferably with `mock`.
- Publish an EL9 RPM first.
- Treat Rocky/Enterprise Linux 10 as a separate future build target.
- Do not build on EL10 and assume the resulting RPM is installable on EL9.

Recommended installed layout:

```text
/usr/bin/scout
/usr/lib/python3.X/site-packages/scout/
/etc/scout/config.toml
/etc/scout/review.schema.json
/usr/lib/systemd/system/scout.service
/var/lib/scout/
/var/lib/scout/state.db
/var/lib/scout/repos/
/var/lib/scout/worktrees/
/var/lib/scout/agents/
```

Logs should go to journald by default. A dedicated `/var/log/scout/` directory is
optional and should only be used if file logs are explicitly configured.

The RPM should install the systemd unit but should not start the service
automatically. Expected operator flow:

```bash
sudo dnf install scout
sudo scout-setup \
  --bitbucket-url https://bitbucket.org/my-workspace/my-repo/pull-requests/ \
  --bitbucket-username-file ./bitbucket_username \
  --bitbucket-api-key-file ./bitbucket_api_key
sudo vi /etc/scout/config.toml
sudo systemctl enable --now scout.service
```

`scout-setup` accepts a Bitbucket Cloud repository or pull-request URL and uses
it to derive the workspace, repository slug, and SSH clone URL. Re-running setup
with another repository URL appends that repository when it is not already
configured, so multi-repository installs can be built up incrementally.

`scout-setup` should detect installed `codex` and `claude` CLIs on its PATH and
write absolute paths into `agents.codex.command` and `agents.claude.command`.
When practical, it should also read the service user's
`~/.codex/config.toml` and copy the Codex agent limit into
`agents.codex.max_subagents`; it should recognize Codex's `[agents] max_threads`
as well as Scout's `max_subagents`. Values below `10` should produce an
operator warning. For the default dedicated
`scout` user, setup should create `/var/lib/scout/.ssh/id_ed25519` when absent,
print the public key, and instruct the operator to add it to Bitbucket as a
read-only repository or workspace access key.

### Packaging Alternatives

Native RPM dependencies are the default for v1 because they fit Enterprise Linux
operations and security update workflows. If Python dependency friction becomes
high, a future packaging profile may install a bundled Python environment under
`/usr/libexec/scout/`, but that should be a deliberate packaging variant rather
than the initial default.

Provider CLIs are user-selected external tools and should not be hard RPM
dependencies. Scout validates the configured CLI's presence at startup and, in
API auth mode, verifies that the configured provider credential is available.
Scout does not currently pin or enforce provider CLI versions.

## systemd Service

Scout should run as a dedicated service user:

- User: `scout`
- Group: `scout`

Example unit shape:

```ini
[Unit]
Description=Scout Bitbucket PR AI Review Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=scout
Group=scout
Environment=HOME=/var/lib/scout
ExecStartPre=/usr/bin/scout --config /etc/scout/config.toml --check-startup
ExecStart=/usr/bin/scout --config /etc/scout/config.toml
Restart=on-failure
RestartSec=10

StateDirectory=scout
ConfigurationDirectory=scout
LogsDirectory=scout

LoadCredential=bitbucket_username:/etc/scout/secrets/bitbucket_username
LoadCredential=bitbucket_api_key:/etc/scout/secrets/bitbucket_api_key
# Optional when git fetches use this deploy key:
# LoadCredential=bitbucket_ssh_key:/etc/scout/secrets/bitbucket_ssh_key

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/scout /var/log/scout

[Install]
WantedBy=multi-user.target
```

The service should log structured events to stdout/stderr so journald can collect
them. Each review job log should include workspace, repository, PR ID, source
commit, provider, and job ID.

The usual production mode is the dedicated unprivileged `scout` user created by
the RPM. When a selected subscription CLI must reuse the installing user's
existing login state, setup may deliberately run the unit as that user and set
`ProtectHome=false`. That mode is less isolated and should be an explicit
operator choice.

Provider CLI paths are configurable through `agents.codex.command` and
`agents.claude.command`; use absolute paths when the CLI is installed outside
systemd's default `PATH`.

## Configuration

Configuration should be TOML and should not contain secrets.

Example:

```toml
[service]
worker_id = "reviewer-1"
retention_days = 7
state_db = "/var/lib/scout/state.db"
state_dir = "/var/lib/scout"
log_level = "INFO"

[bitbucket]
workspace = "my-workspace"
api_base_url = "https://api.bitbucket.org/2.0"
api_auth = "basic"
api_username_credential = "bitbucket_username"
api_key_credential = "bitbucket_api_key"

[[bitbucket.repositories]]
slug = "repo-a"
clone_url = "git@bitbucket.org:my-workspace/repo-a.git"
# Optional source branch ignore regexes.
# ignored_source_branches = ["^release/"]
# Optional draft PR filter.
# ignore_draft_pull_requests = true

[[bitbucket.repositories]]
slug = "repo-b"
clone_url = "git@bitbucket.org:my-workspace/repo-b.git"

[polling]
enabled = true
interval_seconds = 600
pagelen = 50

[queue]
max_parallel_reviews = 4
job_timeout_seconds = 1800
max_attempts = 3
retry_backoff_seconds = 300

[comments]
critical_enabled = true
# Empty disables native PR comments. Default is ["CRITICAL"].
severities = ["CRITICAL"]

[review]
policy_version = "v1"
schema_path = "/etc/scout/review.schema.json"
max_findings = 100
# Backward-compatible defaults for provider review sizing. Each provider can
# override these keys in its own [agents.<provider>] table.
subagent_small_loc_limit = 150
subagent_medium_loc_limit = 600
subagent_large_loc_limit = 1500
subagent_high_risk_bonus = 1
subagent_max_per_lens = 4

[agents]
# Legacy single-provider selector. Still valid by itself.
strategy = "codex"
# Optional multi-provider selector. When set, Scout queues one job per provider
# for each PR. Supported values are "codex" and "claude".
# providers = ["codex", "claude"]

[agents.codex]
enabled = true
auth_mode = "logged_in"
credential = "codex"
home_dir = "/var/lib/scout/agents/codex/main"
max_parallel = 2
timeout_seconds = 1800
command = "codex"
model = "gpt-5.5"
reasoning_effort = "xhigh"
fast_mode = true
max_subagents = 15
subagent_small_loc_limit = 150
subagent_medium_loc_limit = 600
subagent_large_loc_limit = 1500
subagent_high_risk_bonus = 1
subagent_max_per_lens = 3

[agents.claude]
enabled = false
auth_mode = "logged_in"
credential = "claude"
home_dir = "/var/lib/scout/agents/claude/main"
max_parallel = 2
timeout_seconds = 1800
command = "claude"
model = "claude-sonnet-4-6"
effort = "max"
max_subagents = 20
subagent_small_loc_limit = 150
subagent_medium_loc_limit = 600
subagent_large_loc_limit = 1500
subagent_high_risk_bonus = 1
subagent_max_per_lens = 1

[reports]
# If omitted, these default per selected provider.
# Single-provider configs may still set report_id/title here.

[reports.codex]
# report_id = "scout-codex-v1"
# title = "Codex PR Review"

[reports.claude]
# report_id = "scout-claude-v1"
# title = "Claude PR Review"
```

## Secret Handling

Secrets must not be stored in `config.toml`.

Required v1 credentials:

- Bitbucket username.
- Bitbucket API key.
- SSH key or SSH agent configuration for git fetch access.
- Provider credentials only when selected providers are configured for API-key mode.

Provider authentication has two supported modes:

- `logged_in`: Scout invokes a CLI that is already authenticated on the host.
  This is the default for the current Codex deployment.
- `api`: Scout loads provider credential material from systemd credentials and
  passes only the provider-specific credential/config to the CLI.

The v1 secret provider is systemd credentials only. That is acceptable because
Scout v1 is explicitly a systemd service, and keeping one credential path avoids
extra secret-resolution behavior during the first implementation.

Future providers may include environment variables for local development, file
secrets, Vault, AWS Secrets Manager, GCP Secret Manager, and Azure Key Vault.

For systemd credentials:

```ini
LoadCredential=bitbucket_username:/etc/scout/secrets/bitbucket_username
LoadCredential=bitbucket_api_key:/etc/scout/secrets/bitbucket_api_key
# Optional:
# LoadCredential=bitbucket_ssh_key:/etc/scout/secrets/bitbucket_ssh_key
```

The packaged unit loads only the always-required Bitbucket API credentials.
Optional SSH and provider API credentials are configured with service drop-ins,
for example:

```ini
[Service]
LoadCredential=bitbucket_ssh_key:/etc/scout/secrets/bitbucket_ssh_key
# Required only when agents.codex.auth_mode = "api"
LoadCredential=codex:/etc/scout/secrets/codex
# Required only when agents.claude.auth_mode = "api"
LoadCredential=claude:/etc/scout/secrets/claude
```

The daemon reads secrets from `$CREDENTIALS_DIRECTORY`.
Git fetches should use the dedicated service user's normal SSH key by default.
Installations that prefer a credential-provided deploy key can use
`GIT_SSH_COMMAND` or equivalent explicit SSH configuration pointing at the
loaded `bitbucket_ssh_key` credential.

Agent subprocesses should receive only what they need:

- Provider-specific home/config directory.
- Provider-specific token or config reference only in `api` mode.
- Minimal `PATH`.
- No Bitbucket write credential.
- No SSH deploy key unless the provider specifically requires it, which should
  be avoided.
- No ambient user shell environment.
- No unrelated cloud credentials.

## Bitbucket Polling

Scout should use Bitbucket Cloud PR list endpoints with partial responses and
pagination.

Example:

```text
GET /2.0/repositories/{workspace}/{repo_slug}/pullrequests
  ?state=OPEN
  &pagelen=50
  &fields=values.id,values.title,values.description,values.updated_on,values.source.branch.name,values.source.commit.hash,values.destination.branch.name,next
```

The daemon follows `next` until all open PRs are processed.

After a successful unfiltered open-PR poll, Scout prunes local SQLite state for
PR IDs that are no longer returned by Bitbucket. It removes `pull_request_state`,
`review_jobs`, and report-bootstrap rows for closed PRs, but skips any PR that
still has an active `running` or `publishing` job. This keeps the state DB from
growing indefinitely as repositories accumulate closed PRs. Cleanup is skipped
for repository configs that use `pr_ids`, because the filtered list is not a
complete view of the repository's open PRs.

Primary queue update condition:

- Create or update the single active review job for a PR when the PR source
  commit differs from the last successfully reviewed source commit for the same
  PR, provider, and reviewer identity.
- If a newer commit appears before the older commit is reviewed, replace the
  queued target with the newest commit rather than creating another queue entry.

Report existence should not be checked on every poll. Report APIs are used for:

- One-time local state bootstrap when a PR/provider/review key has no local DB
  record yet. If the deterministic provider report already exists on the current
  PR source commit, Scout records the job as succeeded without queueing a review.
- Publishing review results.
- Manual resync.
- State database recovery.
- Uncertain publish outcome.
- Debug/admin commands.

## Review Identity

Scout must avoid duplicate work while still rerunning reviews when review inputs
change.

The v1 queue identity is one active job per PR, provider, policy, and schema:

```text
workspace
repo_slug
pr_id
reviewer_policy_version
schema_version
provider
```

The job row stores the newest source commit, target branch, destination commit,
merge base, and PR description as mutable target data. This allows multiple
commits on the same PR to collapse into one queue entry while still ensuring
Scout reviews the newest known PR state.

The reviewed report identity is:

```text
workspace + repo_slug + pr_id + source_commit_hash + target_branch +
destination_commit_hash or merge_base_hash + reviewer_policy_version +
schema_version + provider
```

Including target/base information matters because the same source commit can
need a different review if the target branch moves.

## Local Git Checkout Model

Scout should review local checkouts rather than diff text alone. The reviewer
needs access to changed files, neighboring code, imports, tests, configuration,
dependency manifests, and repository conventions.

Layout:

```text
/var/lib/scout/repos/{workspace}/{repo}.git
/var/lib/scout/worktrees/{workspace}/{repo}/pr-{id}-{commit}-job-{job_id}/
/var/lib/scout/locks/{mirror-sha256}.lock
```

Flow:

```bash
git clone --mirror "$REPO_URL" "$MIRROR_PATH"
git -C "$MIRROR_PATH" fetch --prune origin
git -C "$MIRROR_PATH" worktree add --detach "$WORKTREE" "$SOURCE_COMMIT"
```

If `clone_url` is a local git repository path, Scout first verifies it is a git
repository with an `origin` remote and runs `git fetch --prune origin` there.
This keeps operator-provided local clones fresh before Scout mirrors from them.

After review:

```bash
git -C "$MIRROR_PATH" worktree remove "$WORKTREE" --force
```

Mirror clone/fetch and worktree add/remove are protected by a per-mirror lock.
Scout uses an in-process thread lock and an advisory `flock` file under
`state_dir/locks/` so concurrent Codex and Claude jobs cannot race on the same
mirror metadata.

Scout should generate review context before invoking the provider:

- PR metadata.
- PR title and description.
- Source branch and commit.
- Target/destination branch and best-known destination commit.
- Merge base or equivalent base reference when available.
- Changed file list.
- Diff patch.
- Review policy version.
- JSON schema path or inline schema.

This context may be written into a temporary directory or a hidden worktree
directory such as `.scout-review/`. The agent prompt should refer to these files.

## Git Authentication

The git credential used to fetch repositories should not be exposed to provider
subprocesses.

The v1 git authentication method is SSH. The service account should have access
to an SSH key or SSH agent configuration that can fetch the configured
repositories.

Bitbucket API access uses HTTP basic auth with a Bitbucket username and API key,
both loaded through systemd credentials. This API credential is used for PR
metadata and Code Insights publishing, and must not be exposed to provider
subprocesses.

## Review Execution

Each job runs through these phases:

1. Lease a pending job.
2. Refresh a local source clone when `clone_url` is a local repository path.
3. Ensure mirror exists and fetch repository updates under the mirror lock.
4. Create detached worktree at the PR source commit.
5. Generate diff and context files.
6. Use the job's provider-specific runner and configuration.
7. Invoke provider CLI with timeout and minimal environment.
8. Capture stdout and stderr separately.
9. Extract final JSON.
10. Validate against schema.
11. Publish report and annotations.
12. Update state and remove worktree.

If cleanup fails, Scout logs a warning. Cleanup failure does not mark a valid
review as failed after publishing succeeds.

## Agent Providers

Scout v1 implements bounded Codex and Claude runners. The runner interface
should stay small and provider-shaped so Gemini CLI or other tools can be added
later without changing queue and publishing behavior.

Provider runners implement a common interface:

```text
run(worktree, prompt, schema_path, run_dir, is_superseded) -> ProviderResult
```

The interface returns:

- Captured stdout text.
- Captured stderr text.
- Final schema-shaped message text.
- Optional provider diagnostics.

Parsing, validation, Bitbucket translation, and retry classification are owned by
the daemon around the provider runner.

### Codex CLI

Representative command:

```bash
codex exec \
  --enable fast_mode \
  --model gpt-5.5 \
  --config 'model_reasoning_effort="xhigh"' \
  --cd "$WORKTREE" \
  --sandbox read-only \
  --output-schema /etc/scout/review.schema.json \
  --output-last-message "$OUTPUT_FILE" \
  < "$PROMPT_FILE"
```

Useful features:

- `exec` for non-interactive mode.
- `--model` for model selection.
- `--config 'model_reasoning_effort="xhigh"'` for reasoning level.
- `--enable fast_mode` to force fast mode on.
- `--cd` for working directory.
- `--sandbox read-only` for readonly behavior.
- `--output-schema` for schema-constrained final output.
- `--output-last-message` to write the final response to a file.
- `--json` for event streams if Scout later wants richer logging.
- Scout writes the prompt to `codex-prompt.txt` in the run directory and passes
  it on stdin so the prompt and diff do not appear in process argv or command
  logs.

### Claude CLI

Representative command:

```bash
claude -p \
  --output-format json \
  --json-schema "$(cat /etc/scout/review.schema.json)" \
  --tools Task,Read,Grep,Glob \
  --allowedTools Task,Read,Grep,Glob \
  --disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch \
  --effort max \
  --permission-mode dontAsk \
  --no-session-persistence \
  --strict-mcp-config \
  < "$PROMPT_FILE"
```

Useful features:

- `-p` / print mode for headless operation.
- JSON output mode.
- Schema-constrained output where supported by the installed version.
- Scout extracts the schema-shaped review from the Claude JSON `result`
  envelope.
- Scout restricts Claude to read-oriented tools (`Task`, `Read`, `Grep`, and
  `Glob`) and explicitly denies shell, edit, write, and web tools.
- Scout passes `--bare` for API-key auth. Logged-in subscription auth does not
  use `--bare` because that mode disables OAuth and keychain reads.
- Scout writes the prompt to `claude-prompt.txt` in the run directory and passes
  it on stdin so the prompt and diff do not appear in process argv or command
  logs.

### Future: Gemini CLI

Representative command:

```bash
gemini -p "$PROMPT"
```

or:

```bash
gemini --prompt "$PROMPT"
```

Scout should treat Gemini schema adherence as version-dependent until the exact
CLI version and flags are locked down. The daemon must validate output and reject
invalid JSON regardless of provider.

### Provider Startup Checks

At startup, Scout should check each selected provider:

- Provider is enabled.
- Command exists.
- Credential is available when `auth_mode = "api"`.
- Configured home directory can be created when `auth_mode = "api"`.

Scout does not need to pin or enforce provider CLI versions in v1. If any
selected provider fails validation, startup should fail with an actionable
error. Scout does not fall back to another provider.

## Provider Strategy

Supported strategies for v1:

- Single provider: run Codex only.
- Single provider: run Claude only.
- Multiple providers: run Codex and Claude independently for each PR.

Future strategies:

- Fallback chain: try providers in order until one succeeds.
- Future consensus: merge multiple provider findings into one report.

Recommended v1 default:

```text
codex
```

The report ID includes the provider name, for example
`scout-codex-v1`, so additional provider reports can be added later
without colliding with Codex results.

Provider cooldown state is persisted in SQLite. The current implemented statuses
are:

- `available`
- `rate_limited`
- `quota_exhausted`

Rate-limited or quota-exhausted providers enter cooldown and Scout stops
claiming that provider's jobs until the cooldown expires. Startup auth failures
fail the service with an actionable error instead of entering the queue.

The persistent job lease must be at least the job provider timeout plus a small
grace period, so long provider runs are not reclaimed while still within their
configured timeout.

## Agent Prompt

The prompt should instruct the agent to inspect the repository, not just the
diff.

It should include:

- Workspace, repository, and PR ID.
- PR title and description.
- Source branch and commit.
- Target/destination branch and best-known destination/base commit.
- Paths to generated context files.
- Review policy.
- Output schema requirements.
- Instruction not to modify files.
- Instruction not to perform network operations.
- Instruction to report only actionable correctness, security, reliability,
  performance, or test issues.
- Instruction not to invent line numbers.
- Instruction to return exactly one final JSON object matching the schema, with
  no progress/status/placeholder JSON.

Prompt injection risk is lower because repositories are trusted by the operator,
but repository content should still be treated as review input rather than
instructions that override Scout's prompt.

### Provider Subagent Review Workflow

The provider prompt should direct the selected job provider to use five focused
reviewer categories, modeled after an internal `pr-review` workflow:

- correctness
- security
- tests
- performance
- best practices

Scout computes how many subagents to request per category from changed LOC only,
not number of files changed. The global `review.*` values remain
backward-compatible defaults, and each selected provider can override the same
LOC thresholds, high-risk bonus, and maximum subagents per lens under
`agents.<provider>`:

- `<= subagent_small_loc_limit`: 1 subagent per category.
- `<= subagent_medium_loc_limit`: 2 subagents per category.
- `<= subagent_large_loc_limit`: 3 subagents per category.
- Above `subagent_large_loc_limit`: 4 subagents per category.

If the PR description contains a case-insensitive `Risk: high` line, Scout adds
`subagent_high_risk_bonus` subagents per category, capped by the job provider's
`subagent_max_per_lens`. Codex defaults to `3` subagents per category and
`15` total subagents. Claude defaults to `1` subagent per category to keep
subscription token use predictable, and can be raised explicitly. The configured
maximum possible total,
`agents.<provider>.subagent_max_per_lens * 5`, must be less than or equal to
that provider's `max_subagents`; static config validation checks each selected
provider and fails otherwise.

Each subagent reviews the same committed PR diff from its assigned lens. Their
outputs should remain separate until a final merge step deduplicates overlapping
findings, preserves the contributing reviewer lenses, and produces one final
recommendation.

When spawning subagents, the prompt should tell the job provider to keep
the configured provider settings. Codex should keep the current model and
reasoning effort and not override agent type, model, or reasoning effort for
forked subagents. Claude should keep the current model and effort configuration
and not override agent type, model, or effort for forked subagents.

Each subagent should be asked to find all actionable findings it can support
from its lens, not only the first issue or the highest severity issue. The merge
step should continue until every changed file has been considered by the
relevant reviewer lenses.

Findings must be anchored to changed lines in the PR diff. Unchanged code may be
mentioned only when needed to explain the impact of a changed line. Style-only
comments should be dropped unless they create correctness, security, test,
performance, or maintainability risk.

## Agent Output Schema

Scout owns the internal agent schema. For v1, the schema should intentionally
use Bitbucket Code Insights terms for report and annotation fields, but Scout
controls the visible Bitbucket report formatting. The agent provides a
recommendation, report metadata, and line-level findings. The publisher then
builds a deterministic, provider-labeled report summary from the validated
findings instead of publishing the agent's raw prose verbatim.

Example shape:

```json
{
  "recommendation": "request_changes",
  "report": {
    "title": "Codex PR Review",
    "details": "Agent-level summary used for validation and diagnostics.",
    "report_type": "BUG",
    "reporter": "scout",
    "data": [
      {
        "title": "Findings",
        "type": "NUMBER",
        "value": 1
      },
      {
        "title": "Recommendation",
        "type": "TEXT",
        "value": "request_changes"
      }
    ]
  },
  "annotations": [
    {
      "external_id": "finding-001",
      "annotation_type": "BUG",
      "path": "src/example.py",
      "line": 123,
      "summary": "Possible null dereference",
      "details": "Detailed explanation.",
      "severity": "HIGH",
      "result": "FAILED",
      "reviewer": "correctness",
      "confidence": "HIGH",
      "smallest_fix": "Suggested fix."
    }
  ]
}
```

Recommended enums:

- `recommendation`: `approve`, `request_changes`
- `annotation_type`: `BUG`, `VULNERABILITY`, `CODE_SMELL`
- `severity`: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`
- `annotation.result`: `FAILED`
- `reviewer`: `correctness`, `security`, `tests`, `performance`,
  `best-practices`
- `confidence`: `HIGH`, `MEDIUM`, `LOW`

Scout validates Bitbucket-compatible fields, uses the top-level recommendation
for the final report result, uses the report type as metadata, and builds the
published report details from the annotation summaries. Scout-only helper fields
such as `reviewer`, `confidence`, and `smallest_fix` are embedded into readable
annotation details because Bitbucket annotations do not accept them directly.

The v1 schema is stored at `/etc/scout/review.schema.json`. It requires a final
recommendation and maps deterministically to the Bitbucket report result:

- `approve` -> `PASSED`
- `request_changes` -> `FAILED`

Initial schema outline:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["recommendation", "report", "annotations"],
  "properties": {
    "recommendation": {
      "type": "string",
      "enum": ["approve", "request_changes"]
    },
    "report": {
      "type": "object",
      "additionalProperties": false,
      "required": ["title", "details", "report_type", "reporter", "data"],
      "properties": {
        "title": { "type": "string", "minLength": 1 },
        "details": { "type": "string", "minLength": 1 },
        "report_type": { "type": "string", "enum": ["BUG", "SECURITY", "TEST", "COVERAGE"] },
        "reporter": { "type": "string", "const": "scout" },
        "data": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["title", "type", "value"],
            "properties": {
              "title": { "type": "string", "minLength": 1 },
              "type": { "type": "string", "enum": ["BOOLEAN", "DATE", "DURATION", "LINK", "NUMBER", "PERCENTAGE", "TEXT"] },
              "value": { "type": ["string", "number", "boolean"] }
            }
          }
        }
      }
    },
    "annotations": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "external_id",
          "annotation_type",
          "path",
          "line",
          "summary",
          "details",
          "severity",
          "result",
          "reviewer",
          "confidence",
          "smallest_fix"
        ],
        "properties": {
          "external_id": { "type": "string", "minLength": 1 },
          "annotation_type": { "type": "string", "enum": ["BUG", "VULNERABILITY", "CODE_SMELL"] },
          "path": { "type": "string", "minLength": 1 },
          "line": { "type": "integer", "minimum": 1 },
          "summary": { "type": "string", "minLength": 1 },
          "details": { "type": "string", "minLength": 1 },
          "severity": { "type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"] },
          "result": { "type": "string", "enum": ["FAILED"] },
          "reviewer": {
            "type": "string",
            "enum": ["correctness", "security", "tests", "performance", "best-practices"]
          },
          "confidence": { "type": "string", "enum": ["HIGH", "MEDIUM", "LOW"] },
          "smallest_fix": { "type": "string", "minLength": 1 }
        }
      }
    }
  }
}
```

Validation rules:

- Parse final JSON only.
- Enforce schema.
- Reject invalid paths.
- Reject impossible or missing line numbers where line-specific annotations are
  required.
- Reject invalid or incomplete provider output without salvaging partial stream
  output.
- Never publish unvalidated agent output.

## Bitbucket Publishing

Scout publishes one deterministic Code Insights report per reviewed commit,
provider, and policy. Multi-provider configs must use distinct report IDs per
provider; config validation rejects duplicates.

Report ID examples:

- `scout-codex-v1`
- `scout-claude-v1`
- `scout-gemini-v1`
- `scout-codex-policy-2026-05`

Because reports are attached to commits, the same report ID can be reused across
different commits.

Report translation is deterministic. Scout sets the configured title, prefixes
it with the provider name if the title omits the provider, summarizes the
validated findings without commit hashes, and sets `result` from the top-level
recommendation:

```json
{
  "title": "Codex PR Review",
  "details": "Codex reviewed this pull request and found 2 material issues:\n\nBy category:\n- Correctness: 1 issue (High: 1)\n- Tests: 1 issue (Medium: 1)\n\nBy severity:\n- High: 1\n- Medium: 1",
  "report_type": "BUG",
  "reporter": "scout",
  "result": "FAILED",
  "data": [
    {
      "title": "Provider",
      "type": "TEXT",
      "value": "Codex"
    },
    {
      "title": "Findings",
      "type": "NUMBER",
      "value": 2
    },
    {
      "title": "Recommendation",
      "type": "TEXT",
      "value": "Request changes"
    }
  ]
}
```

Annotation translation should mostly pass through each validated annotation,
dropping or embedding Scout-only helper fields that Bitbucket does not accept:

```json
{
  "external_id": "finding-001",
  "annotation_type": "BUG",
  "path": "src/example.py",
  "line": 123,
  "summary": "Possible null dereference",
  "details": "Why it matters:\nDetailed explanation.\n\nSuggested fix:\nCheck for null before dereferencing.\n\nReviewer: Codex / correctness / HIGH confidence",
  "severity": "HIGH",
  "result": "FAILED"
}
```

Publishing is idempotent at the report and annotation level. For each validated
review, Scout PUTs the report, lists existing annotations for that report, PUTs
the current annotations, and DELETEs stale annotations whose `external_id` is no
longer present in the validated output.

Scout can also post selected findings as native Bitbucket PR comments. The
configured `[comments].severities` list controls which annotation severities are
included, using `CRITICAL`, `HIGH`, `MEDIUM`, and `LOW`. The default is
`["CRITICAL"]`. An empty list disables comment posting. The legacy
`[comments].critical_enabled = false` setting is still accepted as shorthand for
an empty severity list when `severities` is omitted. For a single selected
severity the comment starts with `Scout: {Severity} issue found by {provider}:`;
for multiple selected severities it starts with `Scout: Issues found by
{provider}:`. Scout does not deduplicate these comments; each completed review
run may leave a new PR comment so reviewers retain history after Code Insights
reports move to a new commit.

Scout enforces `review.max_findings` during output validation. If the provider
returns too many annotations, the review is rejected rather than truncated and
published.

Annotation volume is not expected to be a practical v1 issue because the
subagent review workflow is intentionally high-signal and normally produces only
a few findings.

## Queue and Concurrency

The queue is persistent in SQLite for v1.

Global concurrency:

```toml
[queue]
max_parallel_reviews = 4
retry_backoff_seconds = 300
```

The default global concurrency is configurable. A conservative starting value of
`4` is appropriate when both Codex and Claude are enabled with provider limits
of `2` each.

Provider concurrency:

```toml
[agents.codex]
max_parallel = 2
```

The queue should collapse multiple source commits for the same PR into one
active queue entry. If a PR receives several commits while Scout is behind, only
the newest commit needs review.

Queue semantics:

- At most one active job exists for a given workspace, repository, PR, policy,
  schema, and provider.
- Worker scheduling claims from the global queue, not by looping providers in
  configured order. The oldest eligible job row is leased first by `created_at`
  and `id`, after filtering for provider-level `max_parallel`, provider
  cooldowns, and per-job retry backoff. A retryable failure uses its failure
  timestamp for queue ordering and is claimed after eligible pending work,
  which pushes failed work to the back of the queue.
- Later PRs can start when an older PR only has jobs for providers that are
  already running, at capacity, or in cooldown. Scout does not add artificial
  idle waiting for a PR group to complete before claiming the next eligible row.
- If the active job has not started, polling updates it to the newest source
  commit.
- If polling sees the same review identity that is already pending, running,
  publishing, or succeeded, it updates mutable PR metadata but does not
  supersede or requeue the job.
- If the active job is already running for an older commit, a newer commit marks
  that job as superseded.
- A superseded running job should be terminated early when practical.
- A superseded job must not publish results for the old commit, even if early
  termination fails and the provider later exits successfully.
- After a superseded job exits, the same queue identity is returned to `pending`
  for the newest commit.

Job states:

- `pending`
- `running`
- `publishing`
- `succeeded`
- `failed_retryable`

`superseded` is a boolean flag on the job row rather than a separate status.
Leased work is represented by `status`, `leased_until`, and `lease_token`.
For `failed_retryable`, `leased_until` is reused as the retry-after timestamp.
Scout holds a nonblocking advisory runtime lock under `state_dir` while the
daemon is running. Startup recovery and the stop-time recovery command only
reset `running` or `publishing` rows after acquiring that lock, so a second
manual Scout process cannot clear live leases from the active daemon.

For explicit test runs, `scout --once --reset-state-db` deletes the configured
SQLite database and WAL/SHM sidecars after acquiring the same runtime lock. This
is intended for validating empty-database bootstrap behavior and is not used by
the systemd unit.

Retryable failures:

- Bitbucket 429 and 5xx responses.
- Network failures.
- Agent temporary failure.
- Provider rate limit or quota cooldown.
- Report publish timeout.

Permanent failures:

- Missing commit.
- Invalid repository configuration.
- Authentication failure requiring operator action.
- Schema-invalid output after the configured maximum attempts.
- Unsupported repository size or file size limit.

SQLite should use WAL mode. Transactions should be short. On startup, Scout
returns abandoned `running` and `publishing` rows to `pending` after acquiring
the runtime lock. The systemd unit also runs a best-effort `ExecStopPost`
recovery command so a normal `systemctl stop` leaves the queue clean. Leases
still expire as a backstop for unusual failures.

## State Model

SQLite is the v1 state store.

Current schema shape:

```sql
create table repositories (
  id integer primary key,
  workspace text not null,
  repo_slug text not null,
  clone_url text not null,
  enabled integer not null default 1,
  unique(workspace, repo_slug)
);

create table pull_request_state (
  id integer primary key,
  workspace text not null,
  repo_slug text not null,
  pr_id integer not null,
  title text,
  description text,
  source_branch text,
  destination_branch text,
  source_commit_hash text,
  destination_commit_hash text,
  merge_base_hash text,
  last_reviewed_commit_hash text,
  last_review_key text,
  last_seen_updated_on text,
  review_status text,
  last_report_id text,
  updated_at text not null,
  unique(workspace, repo_slug, pr_id)
);

create table review_jobs (
  id integer primary key,
  workspace text not null,
  repo_slug text not null,
  pr_id integer not null,
  title text,
  description text,
  source_branch text,
  target_source_commit_hash text not null,
  running_source_commit_hash text,
  destination_branch text,
  destination_commit_hash text,
  merge_base_hash text,
  reviewer_policy_version text not null,
  schema_version text not null,
  provider text not null,
  status text not null,
  superseded integer not null default 0,
  attempts integer not null default 0,
  leased_until text,
  lease_token text,
  target_review_key text not null,
  running_review_key text,
  error_message text,
  created_at text not null,
  updated_at text not null,
  unique(
    workspace,
    repo_slug,
    pr_id,
    reviewer_policy_version,
    schema_version,
    provider
  )
);

create table provider_state (
  provider text primary key,
  status text not null,
  cooldown_until text,
  last_error text,
  updated_at text not null
);
```

This schema keeps one active job row per PR/provider/review configuration and
updates `target_source_commit_hash` to the newest commit. If a worker starts a
job, it copies `target_source_commit_hash` to `running_source_commit_hash` and
`target_review_key` to `running_review_key`. A poll that sees the same
`target_review_key` updates mutable PR metadata and returns without requeueing.
A poll that sees a newer review key while the job is running updates the target
fields and sets `superseded = 1`; the worker must then discard the stale result
and return the row to `pending` for the latest commit.

Queue claim and enqueue/update paths use `BEGIN IMMEDIATE` transactions to
serialize SQLite writers and avoid duplicate claims or duplicate active jobs.

## Readonly and Resource Controls

Because reviewed repositories are trusted by the operator, v1 does not require a
full hostile-code sandbox. Scout should still use layered controls:

- Detached worktree with readonly permissions where practical.
- Provider readonly/sandbox mode where supported.
- Dedicated service user.
- Minimal subprocess environment.
- No Bitbucket write token in provider subprocesses.
- Per-provider job timeout. Codex and Claude both default to 1800 seconds.
- Optional CPU and memory limits through systemd or future per-job execution.

Potential future hardening:

- `bubblewrap`
- `firejail`
- rootless Podman
- `systemd-run` transient per-job units
- readonly bind mounts
- network-disabled execution
- seccomp restrictions

## Git Safety Controls

Scout should avoid surprising git behavior:

- Do not run repository hooks.
- Avoid inherited credential helpers.
- Keep git credentials outside provider environments.
- Do not initialize submodules in v1.
- Do not fetch Git LFS objects in v1.

Submodules and LFS are out of scope for v1.

## Failure Handling

Scout should classify failures so operators can distinguish temporary runtime
problems from configuration problems. PR-level jobs should not become permanently
dead because of Git, DNS, SSH, or fetch instability. Persistent repository
configuration problems should be detected during setup or startup validation
rather than represented as permanent PR queue rows. The systemd unit uses
`scout --check-startup` before starting the daemon; that validates static config,
Bitbucket repository access, Git clone access, and provider startup checks.

Examples:

- Bitbucket 401/403: setup/startup validation failure until credentials or
  permissions are fixed.
- Bitbucket 429: retry after cooldown.
- Bitbucket 5xx: retry with backoff.
- Git network, DNS, SSH, fetch, or missing commit failures during review:
  retryable with backoff. Setup validation should catch persistent repository
  URL or SSH key misconfiguration before the service is started.
- Provider auth failure at startup: fail startup with an actionable error.
- Provider quota/rate limit: mark provider cooldown.
- Provider timeout: retryable with backoff.
- Invalid JSON or schema-invalid output: retryable with backoff.
- Invalid annotation location: reject the provider output. Scout does not
  publish partially valid provider output.

Retryable failures update the job timestamp and set a retry-after delay, which
pushes that job behind other eligible queue work. Provider quota and rate-limit
errors set a provider cooldown and defer that provider's jobs without consuming
another attempt. Runtime PR jobs do not become permanent solely because they
reached the configured attempt limit.

## Observability

Scout should log structured events for:

- Poll cycle started/completed.
- Repository poll result.
- PR detected or changed.
- Job enqueued.
- Job claimed/running.
- Git fetch/worktree creation.
- Provider invocation started/completed.
- Output validation success/failure.
- Report publish success/failure.
- Annotation publish success/failure.
- Job succeeded/failed.
- Provider state changes.

Scout should log provider CLI inputs and outputs for operability:

- Prompt/context paths. Prompt text is written to per-run files and is not
  included in command argv.
- Provider command arguments after secret redaction.
- Provider stdout/stderr file paths.
- Parsed review metadata and local review-log entry.

Provider I/O logs must never include Bitbucket API credentials, SSH key material,
or provider API keys.

Scout also appends a durable local review record to
`state_dir/review-log.jsonl` after provider output validates and before
publishing starts. Each JSONL entry records timestamp, provider, workspace,
repository, PR, commit, recommendation, finding counts by reviewer and severity,
normalized provider token usage when available, and paths to raw provider
stdout/stderr logs. It stores paths and metadata, not raw provider log contents
or credential material.

Provider-attempt usage is also written to `state_dir/provider-usage.jsonl`.
Unlike the validated review log, this attempt log is written immediately after
the provider attempt finishes, so token consumption remains attributable to a
PR/provider even when schema validation or Bitbucket publishing later fails.
Each entry records timestamp, workspace, repo, PR, commit, provider, job id,
attempt number, status, raw log paths, and normalized usage when the provider
reported it.

Usage normalization is provider-specific:

- Claude: parse the CLI JSON envelope, preferring `modelUsage` totals because
  that captures subagent/model fan-out better than the top-level final-message
  usage field. Store input, output, cache creation, cache read, total tokens,
  model-level breakdown, and reported cost when present.
- Codex: parse the CLI's `tokens used` output when present. Store total tokens
  only unless Codex exposes a richer structured usage format later.

Cost is best-effort metadata. Relative expense should primarily use
`total_tokens` because subscription/logged-in modes may not report meaningful
per-run cost.

Local review log entries, provider usage records, and raw provider run
directories are retained for at most `service.retention_days`, defaulting to 7
days. Operators can compare PR/provider usage with `scout --usage-summary`, plus
optional `--repo` and `--pr` filters.

v1 can rely on journald. Later phases may add:

- Prometheus metrics endpoint.
- Prometheus textfile collector output.
- Admin CLI for resync, retry, and diagnostics.

## Security and Compliance Notes

Scout sends private repository content to the configured AI provider CLIs unless
those CLIs are backed by local models or an approved enterprise endpoint. This is
an explicit deployment consideration, not something Scout can hide.

Operators must ensure:

- Provider terms allow this use.
- Repository owners approve sending code to the chosen providers.
- Credentials are scoped appropriately.
- Logs do not contain secrets.
- Agent raw outputs and review audit entries are retained for no more than
  seven days by default and by configuration validation.

Credential rotation is deferred to a later phase. Any future credential pool
must be designed around legitimate account ownership and provider terms, not
quota bypassing.

## Phase Plan

### Phase 1: MVP

- Python daemon.
- EL9 native RPM.
- systemd service.
- TOML config.
- systemd credentials.
- Bitbucket polling with pagination and partial responses.
- SQLite state database and persistent queue.
- Queue collapse so only the newest commit per PR/provider/policy is reviewed.
- Superseded running reviews are terminated early when practical.
- Local git mirror and detached worktree.
- Freshening of local-clone `clone_url` sources before mirror updates.
- Per-mirror locking around clone, fetch, worktree add, and worktree removal.
- SSH git fetch authentication.
- Bitbucket basic auth with username/API key for API and Code Insights.
- Codex runner.
- Claude runner.
- Optional multi-provider mode that runs Codex and Claude independently for the
  same PR and publishes provider-specific reports.
- Codex supports `logged_in` and `api` authentication modes.
- Claude supports `logged_in` and `api` authentication modes.
- Codex review prompt using correctness, security, tests, performance, and
  best-practices categories with LOC-scaled subagents.
- Strict JSON schema validation.
- Report result is `FAILED` when recommendation is `request_changes`, otherwise
  `PASSED`.
- Code Insights report publishing with provider-specific report IDs.
- Basic annotations.
- Configurable review concurrency, defaulting to 2 global workers.
- Provider-specific concurrency caps.
- Provider cooldown detection for usage-limit lockouts.
- Durable local review log and one-week retention for review artifacts.
- Per-PR/provider token usage logging and local usage summary command.
- Journald logging.

### Phase 2: Operational Hardening

- Separate webhook receiver.
- Gemini runner support.
- Configurable fallback provider strategy.
- Stronger sandboxing.
- Admin resync and manual retry commands.
- Metrics.
- Repo-level review config.
- Better quota and rate-limit classification.
- Report stale detection.
- More robust annotation reconciliation.

### Phase 3: Credential Pools

- Multiple credentials per provider.
- Policy-based credential eligibility.
- Per-credential concurrency.
- Per-credential cooldown.
- Quota-aware scheduling.
- Explicit compliance model for user OAuth credentials and API key pools.

## Deferred Decisions and Assumptions

The following future options and implementation assumptions should remain
visible, but they do not block the v1 build.

### Bitbucket API Permission Assumption

Bitbucket calls these credential permissions or scopes depending on credential
type. In this document, "scope" means the permissions granted to the Bitbucket
API key. For v1, assume the configured Bitbucket username/API key has all
permissions Scout needs:

- Read PR metadata.
- Read repository metadata.
- Publish Code Insights reports.
- Publish annotations.
- Write PR comments for critical findings.

Git fetch uses SSH separately, so the API key does not need to be used for clone
or fetch operations.

### Future Provider Strategy

v1 can run one configured provider or both Codex and Claude. Future options:

- Fallback chain across configured providers.
- Merged consensus across multiple providers.
- Provider chosen per repository.

### Sandboxing Depth

Minimum v1:

- Readonly worktree.
- Provider readonly mode where supported.
- Allowlisted environment.
- Timeouts.
- No Bitbucket write token in agent environment.

Future stronger isolation:

- Per-job container or sandbox.
- Network disabled for provider jobs.
- CPU and memory isolation.
- Separate transient runtime user.

### Queue Backend

v1 uses SQLite. Multi-node deployments should revisit PostgreSQL or an external
queue such as Redis, NATS, or SQS.

### RPM Dependency Strategy

v1 uses native EL9 RPM packaging. A bundled Python application layout may be
revisited if dependency availability becomes a major operational problem.
