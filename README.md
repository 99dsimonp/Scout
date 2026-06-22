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
- Optional native PR comments for selected severities.
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

Build the RPM from a checkout:

```bash
mkdir -p ~/rpmbuild/SOURCES
git archive --format=tar.gz --prefix=scout-0.1.0/ \
  -o ~/rpmbuild/SOURCES/scout-0.1.0.tar.gz HEAD
rpmbuild -ba packaging/scout.spec
```

Install the built package:

```bash
sudo dnf install -y ~/rpmbuild/RPMS/noarch/scout-0.1.0-1.el9.noarch.rpm
scout --config /etc/scout/config.toml --check-config
```

The RPM installs `/usr/bin/scout`, `/usr/bin/scout-setup`,
`/etc/scout/config.toml`, `/etc/scout/review.schema.json`, and the systemd unit
at `/usr/lib/systemd/system/scout.service`.

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
`/etc/scout`, copy the example config and schema if missing, create
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
`LoadCredential=` for `bitbucket_username`, `bitbucket_api_key`, and the SSH key
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

The packaged unit loads the always-required Bitbucket credentials. For each
selected provider that uses `auth_mode = "api"`, add a systemd drop-in for that
provider credential,
for example:

```ini
[Service]
LoadCredential=claude:/etc/scout/secrets/claude
```

Scout classifies PR description risk with a configured agent, then combines that
with changed LOC. `low` risk always uses 1 reviewer per category. `medium` risk
uses LOC sizing: 1 reviewer per category up to 150 changed lines, 2 up to 600,
3 up to 1500, and 4 above that. `high` risk adds
`subagent_high_risk_bonus` per category before caps. Disabled or failed risk
classification defaults to `medium`. Codex caps this at 3 reviewers per
category by default, so large PRs use at most 15 Codex subagents.

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
impact, suggested fix, and reviewer metadata. When a report is republished,
Scout removes stale annotations whose `external_id` is no longer present in the
latest validated review output.

Native PR comments are controlled by `[comments].severities`. The default is
`["CRITICAL"]`; configure any subset of `CRITICAL`, `HIGH`, `MEDIUM`, and `LOW`,
or an empty list to disable comments. The legacy
`[comments].critical_enabled = false` setting is still accepted when
`severities` is omitted.

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
