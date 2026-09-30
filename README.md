# Scout

Scout is a self-hosted AI review service for Bitbucket Cloud pull requests. It
watches open PRs, checks out the exact commit under review, runs Codex and/or
Claude in headless mode, validates findings against a strict JSON schema, and
publishes the result back to Bitbucket Code Insights.

It is built for teams that want automated review coverage without turning CI
into the reviewer. Scout runs as a Linux service, keeps queue and review state
in SQLite, stores secrets through systemd credentials, uses local read-only
worktrees, and can run multiple providers independently on the same PR.

Scout provides:

- Code Insights reports and inline annotations for each reviewed commit.
- An alternate inline-comment mode that posts one native code comment per finding.
- Optional native PR comments for selected severities in report mode.
- Retry and cooldown handling for provider failures and usage-limit lockouts.
- Local audit logs and provider usage summaries with short retention.

The detailed design is captured in [DESIGN.md](DESIGN.md).

## Installation

Scout is packaged for Rocky Linux 9 / Enterprise Linux 9 as an RPM. Build
dependencies from Rocky's CodeReady Builder-compatible repository are required,
so enable CRB before installing the RPM build toolchain:

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --set-enabled crb
sudo dnf install -y \
  rpm-build python3-devel pyproject-rpm-macros python3-wheel \
  python3-setuptools python3-tomli systemd-rpm-macros \
  git tar gzip openssh-clients shadow-utils systemd
```

On Rocky Linux 10 / Enterprise Linux 10, omit `python3-tomli`; Scout uses
Python's standard-library `tomllib` there.

Build the RPM from a checkout:

```bash
mkdir -p ~/rpmbuild/SOURCES
git archive --format=tar.gz --prefix=scout-0.1.0/ \
  -o ~/rpmbuild/SOURCES/scout-0.1.0.tar.gz HEAD
rpmbuild -ba packaging/scout.spec
```

Install the built package:

```bash
sudo dnf install -y ~/rpmbuild/RPMS/noarch/scout-0.1.0-4.el9.noarch.rpm
scout --config /etc/scout/config.toml --check-config
```

The RPM installs `/usr/bin/scout`, `/usr/bin/scout-setup`,
`/etc/scout/config.toml`, `/etc/scout/review.schema.json`, and the systemd unit
at `/usr/lib/systemd/system/scout.service`.
RPM upgrades preserve `config.toml` but replace the bundled review schema so new
reviewer values cannot be rejected by a stale local copy.

### Upgrading a running instance

Upgrade the installed package with `dnf upgrade /path/to/scout-<version>-<release>.rpm`.
The RPM preserves `/etc/scout/config.toml` and does not replace the SQLite
database at `service.state_db` (default `/var/lib/scout/state.db`). Keep the same
`service.state_db` and `service.state_dir` paths when upgrading. Completed
reviews and processed review-request comments remain in that database, so an
ordinary package upgrade does not queue fresh reviews of unchanged open PRs.

Startup migrates older databases in place, including report identities written
before inline-comment mode was introduced. It returns interrupted running or
publishing jobs to the queue and keeps completed jobs completed. Package and
bundled schema updates do not automatically change `review.policy_version`.
Changing the configured policy, provider, or output mode is a separate review
decision; report mode also reacts to changes in the PR's commits and target.

Before upgrading, stop the service and back up the configured database and
config. Copy any SQLite `-wal` and `-shm` sidecars together with the database
while the service is stopped. Upgrade the RPM, then start the service again:

```bash
sudo systemctl stop scout
# Back up the configured database, its sidecars, and /etc/scout/config.toml.
sudo dnf upgrade -y /path/to/scout-0.1.0-4.el9.noarch.rpm
sudo systemctl start scout
```

`scout-setup` is not required for an ordinary RPM upgrade. Do not use
`--reset-state-db` during deployment: it deliberately deletes review history.
Audit logs and provider run files still follow the configured retention window;
that cleanup does not remove completed-review records for open PRs.

## Development

Run tests with the standard library test runner:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Run one polling/review pass:

```bash
PYTHONPATH=src python3 -m scout --config config/config.toml.example --once
```

Run one pass after deleting the configured SQLite state database:

```bash
PYTHONPATH=src python3 -m scout --config config/config.toml.example --once --reset-state-db
```

`--reset-state-db` is intended for explicit test runs and requires `--once`.
It refuses to run while another Scout process holds the runtime lock.

The example config is not directly runnable until credentials and repositories
are configured.

Validate static configuration without contacting Bitbucket or selected providers:

```bash
PYTHONPATH=src python3 -m scout --config config/config.toml.example --check-config
```

## systemd Setup

The RPM installs the packaged unit and the `scout-setup` helper. For a source
checkout, use `scripts/setup.sh`. The helper can install a systemd unit, create
`/etc/scout`, copy the example config if missing, refresh the bundled default
schema, create
`/var/lib/scout` and `/var/log/scout`, and install Bitbucket credentials as
systemd credential source files:

```bash
# RPM install:
sudo scout-setup \
  --bitbucket-url https://bitbucket.org/my-workspace/my-repo/pull-requests/ \
  --bitbucket-username-file ./bitbucket_username \
  --bitbucket-api-key-file ./bitbucket_api_key

# Source checkout:
sudo scripts/setup.sh \
  --bitbucket-url https://bitbucket.org/my-workspace/my-repo/pull-requests/ \
  --bitbucket-username-file ./bitbucket_username \
  --bitbucket-api-key-file ./bitbucket_api_key
```

Bitbucket OAuth client-credentials auth is also supported for service accounts.
Use these flags instead of the username/API-key flags:

```bash
sudo scout-setup \
  --bitbucket-url https://bitbucket.org/my-workspace/my-repo/pull-requests/ \
  --bitbucket-oauth-client-id-file ./bitbucket_oauth_client_id \
  --bitbucket-oauth-client-secret-file ./bitbucket_oauth_client_secret
```

This writes `bitbucket.api_auth = "oauth_client_credentials"` and loads
`bitbucket_oauth_client_id` / `bitbucket_oauth_client_secret` as systemd
credentials. For manual configuration, set the same `api_auth` value in
`/etc/scout/config.toml`.

By default the service runs as the dedicated `scout` user. That is the usual
service pattern: the daemon gets its own unprivileged identity, state directory,
and credential set instead of inheriting the installing user's shell. If a
selected agent CLI must reuse the invoking user's existing logged-in
subscription/session, opt in explicitly:

```bash
# RPM install:
sudo scout-setup --logged-in-cli-current-user

# Source checkout:
sudo scripts/setup.sh --logged-in-cli-current-user
```

That mode is less isolated because the service runs as your login user and can
read your home directory. In dedicated-user mode, log the CLI in under the
`scout` account or use provider `api` auth.

The optional Bitbucket SSH deploy key can be installed with
`--bitbucket-ssh-key-file ./id_bitbucket`. The generated unit uses
`LoadCredential=` for the selected Bitbucket API credentials and the SSH key
when present. In the default dedicated-user mode, setup also creates
`/var/lib/scout/.ssh/id_ed25519` when absent and prints the public key to add to
Bitbucket as a read-only access key. Edit `/etc/scout/config.toml` before
starting the service.

`--bitbucket-url` accepts a repository URL or Bitbucket Cloud pull-request URL
and derives `workspace`, repository `slug`, and the SSH clone URL. Running setup
again with another repository URL appends another `[[bitbucket.repositories]]`
block when it is not already present.

Provider CLI binaries are configured in TOML. Setup writes absolute `codex` and
`claude` paths when it can detect them. Otherwise, set absolute paths when the
CLI is installed outside systemd's default `PATH`, for example:

```toml
[agents.claude]
command = "/home/linuxbrew/.linuxbrew/bin/claude"
```

When `/var/lib/scout/.codex/config.toml` is readable, setup also copies Codex's
agent limit into Scout config. It understands Codex's `[agents] max_threads`
setting and Scout's `max_subagents` setting, and warns when the detected value
is below 10.

## Multiple Repositories

Add one `[[bitbucket.repositories]]` block per repository:

```toml
[[bitbucket.repositories]]
slug = "repo-a"
clone_url = "git@bitbucket.org:my-workspace/repo-a.git"

[[bitbucket.repositories]]
slug = "repo-b"
clone_url = "git@bitbucket.org:my-workspace/repo-b.git"
```

Scout polls each configured repository and keeps queue entries isolated by
workspace, repository, PR ID, policy, schema, and provider. For live testing, add
`pr_ids = [123]` to a repository block to limit reviews to specific PRs.
You can also skip branch namespaces with regexes by adding
`ignored_source_branches`, for example `["^release/"]` to ignore release
source branches, or `ignored_target_branches` to ignore PRs targeting matching
destination branches.
To process only non-draft PRs, set `ignore_draft_pull_requests = true` for the
repository block.

Repositories can also provide read-only context for a review:

```toml
[[bitbucket.repositories]]
slug = "app"
clone_url = "git@bitbucket.org:my-workspace/app.git"
related_repositories = ["contracts"]

[[bitbucket.repositories]]
slug = "contracts"
clone_url = "git@bitbucket.org:my-workspace/contracts.git"
review_enabled = false
context_ref = "master"
```

`related_repositories` names other configured repository blocks. Scout fetches
each directly listed repository for every review job, resolves `context_ref` to
an exact commit, and gives that detached, read-only worktree to the reviewer.
When `context_ref` is omitted, Scout uses the remote repository's default
branch. Related relationships are not expanded recursively. A repository with
`review_enabled = false` is not polled for pull requests, although startup still
validates its Bitbucket repository and clone URL because review jobs may need it.

Related repositories must be disclosure-compatible trust domains. Scout sends
their contents to the configured AI provider as review context. The provider may
quote that content in its response; Scout may then reproduce those excerpts in
Bitbucket reports or comments and retain them in raw files under
`state_dir/runs/`. Only relate a repository when its content may be disclosed to
every contributor or viewer who can read review output in the primary
repository. Prompt instructions tell the reviewer how to use related code, but
they do not enforce this confidentiality boundary.

Related branch changes do not alter review identity and do not automatically
rerun an unchanged PR. Bump `review.policy_version` when enabling or changing
related repository context so existing PR commits are reviewed under the new
policy.

Scout schedules six review lenses: correctness, security, tests, performance,
best practices, and compatibility. The compatibility lens checks changed
cross-repository contracts and rollout-order constraints against related code.
Version-skew findings must be grounded in visible interfaces, compatibility
shims, versioning or deprecation policy, tests, documentation, or other
repository evidence. The configured related checkout represents one exact
revision, so Scout does not assert behavior for unavailable older or newer
versions. Without related repositories, this lens checks only externally
consumed interfaces, configuration, and data formats visible in the primary
repository.

Workers claim the oldest eligible row from the global queue after filtering for
provider capacity and cooldowns. A running or cooling-down provider does not
force other eligible providers to sit idle behind an older PR.

## Agent Provider Settings

Scout supports the legacy single-provider selector:

```toml
[agents]
strategy = "codex" # or "claude"
```

To run multiple providers for every PR, add `providers`:

```toml
[agents]
strategy = "codex" # optional legacy primary; must be included in providers
providers = ["codex", "claude"]

[agents.claude]
enabled = true
```

There is no provider fallback. The daemon queues, validates, and runs one job
per selected provider. Claude is disabled by default; selecting it as a review
provider or risk provider requires `agents.claude.enabled = true`.

Codex behavior is configured under `[agents.codex]`:

```toml
command = "codex"
model = "gpt-5.5"
reasoning_effort = "xhigh"
fast_mode = true
```

Scout passes these as `--model`, `model_reasoning_effort`, and the `fast_mode`
feature flag when invoking `codex exec`.

Claude behavior is configured under `[agents.claude]`:

```toml
enabled = false
auth_mode = "logged_in" # or "api"
command = "claude"
model = "claude-sonnet-4-6" # optional; leave empty for the CLI default
effort = "max" # low, medium, high, xhigh, max; leave empty to omit
```

Scout invokes Claude in print mode with `--output-format json` and
`--json-schema`, passes `--model` and `--effort` when configured, then extracts
the schema-shaped review from Claude's `result` envelope. It restricts Claude to
read-oriented tools (`Task`, `Read`, `Grep`, and `Glob`) and explicitly denies
shell, edit, write, and web tools. In `api` auth mode it passes
`ANTHROPIC_API_KEY` from the configured systemd credential, sets `HOME` to
`agents.claude.home_dir`, and uses `--bare`. In `logged_in` mode it uses the
current `HOME` so the CLI can read its existing subscription login.

If a provider CLI reports an account usage-limit lockout, Scout records a
provider cooldown in SQLite and stops claiming that provider's jobs until the
default five-hour cooldown expires.
Job leases are automatically extended to at least the job provider timeout plus
a small grace period, so a long provider run is not reclaimed while it is still
within its configured timeout.
Scout holds a runtime lock while active. On startup, and after normal systemd
stops, it returns abandoned `running` or `publishing` rows to `pending` only
when that lock proves no other Scout daemon is using the state directory.
When running under systemd, set `agents.codex.command` or
`agents.claude.command` to an absolute CLI binary path if the service PATH does
not include the provider CLI.

The packaged unit loads Basic Bitbucket credentials by default. When using
`bitbucket.api_auth = "oauth_client_credentials"` without `scout-setup`, add a
systemd drop-in that resets `LoadCredential=` and loads
`bitbucket_oauth_client_id` plus `bitbucket_oauth_client_secret`. For each
selected provider that uses `auth_mode = "api"`, add a systemd drop-in for that
provider credential,
for example:

```ini
[Service]
LoadCredential=
LoadCredential=bitbucket_oauth_client_id:/etc/scout/secrets/bitbucket_oauth_client_id
LoadCredential=bitbucket_oauth_client_secret:/etc/scout/secrets/bitbucket_oauth_client_secret
LoadCredential=claude:/etc/scout/secrets/claude
```

Scout classifies PR description risk with a configured agent, then combines that
with changed LOC. `low` risk always uses 1 reviewer per category. `medium` risk
uses LOC sizing: 1 reviewer per category up to 150 changed lines, 2 up to 600,
3 up to 1500, and 4 above that. `high` risk adds
`subagent_high_risk_bonus` per category before caps. Disabled or failed risk
classification defaults to `medium`. Codex caps this at 3 reviewers per
category by default, so large PRs use at most 18 Codex subagents.
For each provider, Scout also caps reviewers per lens at
`max_subagents / 6`, rounded down. This keeps configurations created before the
compatibility lens valid: `max_subagents = 15` and
`subagent_max_per_lens = 3` now run 2 reviewers per lens (12 total). At least 6
total subagents are required so every lens has one reviewer.

Risk classification is enabled by default and uses Codex unless configured
otherwise:

```toml
[review.risk]
enabled = true
provider = "codex" # must name an enabled configured agent
model = "gpt-5.4"
effort = "low" # Codex reasoning effort, or Claude --effort
timeout_seconds = 120
```

When `provider = "claude"` and `model` is omitted, Scout defaults the risk
classifier model to the normal Claude Sonnet default. Because Claude is disabled
by default, this also requires `agents.claude.enabled = true`.

The global `review.*` sizing values are backward-compatible defaults; Codex and
Claude can each override the LOC thresholds, high-risk bonus, and
`subagent_max_per_lens` under their `[agents.<provider>]` table. Claude defaults
to one subagent per category to keep token use predictable unless you opt in to
more fan-out. Each selected provider's `max_subagents` is the hard total limit
validated at config load and before each review.

## Review quality and existing discussions

The best-practices lens checks whether the PR adds unused code or removes the
last use of existing code. It must inspect callers, exports, callbacks,
registrations, and other supported entry points before reporting dead code.
The finding names the unused code and anchors the change that made it unused,
including a removed call on the old side of the diff.

The same lens searches for existing implementations before flagging duplicate
code. For example, a new local test helper may duplicate a framework helper, or
a new C utility may repeat an existing function. The reviewer must name the
existing implementation and show that its behavior, dependencies, and scope make
reuse appropriate. Similar syntax alone is not a finding.

The tests lens checks what each changed test proves. It flags duplicate coverage,
tests that only prove a test or pipeline ran, and excessive checks of test
scaffolding when they add no distinct regression coverage. Findings must identify
the existing coverage or CI signal and explain why the new check adds no useful
protection. Tests of framework or pipeline behavior are useful when that behavior
is the product under test; different inputs, failure paths, or integration
boundaries can also justify separate tests.

Before each review, Scout fetches all pages of the PR's Bitbucket comments and
adds the bodies, authors, reply relationships, timestamps, and inline locations
to the read-only review context. The reviewer uses them as evidence, not as
instructions. It suppresses a repeated issue only when a developer reply clearly
declares that same issue out of scope. An existing comment, a resolved thread,
or an ambiguous reply alone does not suppress a finding. Other issues remain
eligible even if they occur in the same file or function. If comment retrieval
fails, Scout retries the job before starting the review instead of treating the
discussion as empty.

These checks apply to both providers on future review runs. They do not change
the saved review identity or automatically rerun completed PRs after upgrade.
Detection and interpretation of replies are performed by the configured review
model using the supplied code and discussion evidence.
The bundled review schema includes `finding_kind` (`general`, `dead_code`,
`duplicate_code`, or `low_value_test`). RPM upgrades replace that bundled schema.
If you use a custom `review.schema_path`, add this field there too so the provider
can identify dead-code findings for the mandatory warning comments.

## Bitbucket Reports

Scout publishes provider-specific reports. If report settings are omitted, the
default IDs and titles are `scout-codex-v1` / `Codex PR Review` and
`scout-claude-v1` / `Claude PR Review`. Single-provider configs may
still set `[reports].report_id` and `[reports].title`. Multi-provider configs
must use provider tables such as `[reports.codex]` and `[reports.claude]` for
custom report IDs or titles. If a configured title omits the provider, Scout
prefixes it before publishing. Report details summarize the validated findings
without commit hashes by category and severity counts instead of enumerating
every finding. Annotation details are reformatted into readable sections for
impact, suggested fix, and reviewer metadata. Code Insights annotation summaries
are capped at 450 characters and details at 2,000 characters after formatting.
Longer text ends with `...`; native inline comments use their separate comment
limit. When a report is republished,
Scout removes stale annotations whose `external_id` is no longer present in the
latest validated review output.

Every finding declares whether its line number belongs to the changed file
(`NEW`) or the original file (`OLD`). Inline-comment mode supports both sides,
including deletion-only findings. Code Insights reports are attached to the
source commit and Scout's current annotation payload has no old-side anchor, so
report mode sends only valid `NEW` findings as Code Insights annotations.
Dead-code findings on changed `OLD` lines are retained for a PR comment and
included in the report's counts and result; they are never sent as an old-side
Code Insights annotation. Other `OLD` findings in report mode, and findings that
do not identify a changed line on their declared side, are discarded individually.
Recommendations, details, and counts use only the
remaining findings; if none remain, Scout publishes a passing no-findings result.

Native PR comments are controlled by `[comments].severities`. The default is
`["CRITICAL"]`; configure any subset of `CRITICAL`, `HIGH`, `MEDIUM`, and `LOW`,
or an empty list to disable severity-selected comments. Each dead-code warning
gets its own bounded PR comment, regardless of this severity selection, so a
long unrelated finding cannot hide it. In inline-comment mode they use the
normal inline finding comments. The legacy
`[comments].critical_enabled = false` setting is still accepted when
`severities` is omitted.

To use native inline comments instead of Code Insights reports, set:

```toml
[review]
output_mode = "inline_comments"

[review.request_comments]
provider = "codex"
model = "gpt-5.4"
effort = "low"
timeout_seconds = 120
```

Inline comment mode reviews each non-draft PR once per configured provider. New
commits do not automatically trigger another review; a developer can request one
by mentioning `@scout` or `@Scout` in a PR comment. Scout classifies tagged
comments with `review.request_comments` before queueing a rerun. In this mode,
`[comments].severities` and `[comments].critical_enabled` are ignored: every
validated annotation with a valid changed-line location on its declared `NEW`
or `OLD` side is posted as its own inline code comment, and no PR-level fallback
comment is posted.

## Local Review Log

After a provider result validates and before Scout publishes it, Scout appends a
local audit record to `state_dir/review-log.jsonl`. Each JSONL entry contains
provider, repository, PR, commit, recommendation, finding counts, normalized
provider token usage when available, and paths to the raw provider stdout/stderr
logs. It does not include raw log contents or credential material.

Scout also appends provider-attempt usage records to
`state_dir/provider-usage.jsonl`. This is written as soon as a provider attempt
finishes, so token use remains visible even if later validation or Bitbucket
publishing fails. Claude usage is parsed from the CLI JSON envelope, including
model-level token and cost fields when present. Codex usage is parsed from the
CLI's `tokens used` output when present. Cost is best-effort and may be zero for
providers or auth modes that do not report it.

To compare expensive PRs locally:

```bash
scout --config /etc/scout/config.toml --usage-summary
scout --config /etc/scout/config.toml --usage-summary --repo repo-a --pr 1166
```

Scout keeps local review log entries and raw provider run directories under
`state_dir/runs` for at most `service.retention_days`, which defaults to 7 and
cannot be configured above 7. Provider usage records follow the same retention
window.

After each successful unfiltered poll of open Bitbucket PRs, Scout prunes SQLite
state for PRs that are no longer open. It keeps closed PR rows while they still
have an active `running` or `publishing` job, then removes their PR state,
review jobs, and report-bootstrap rows on a later poll. During every poll, Scout
also removes queued jobs and bootstrap rows for PRs currently ignored by
repository `ignored_source_branches` or `ignored_target_branches`, and draft PRs when
`ignore_draft_pull_requests` is enabled.

## License

Scout is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
