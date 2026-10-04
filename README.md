# Scout

**Self-hosted AI code review for Bitbucket Cloud pull requests.**

Scout watches open PRs, checks out the exact commit under review, runs
[Codex](https://github.com/openai/codex) and/or [Claude Code](https://claude.com/claude-code)
headlessly, validates their findings against a strict JSON schema, and posts the
results back to Bitbucket.

- **Two output modes:** Code Insights reports with annotations, or native inline PR comments.
- **Multi-provider:** run Codex and Claude on the same PR; inline mode merges and deduplicates their findings.
- **Six review lenses:** correctness, security, tests, performance, best practices, and cross-repo compatibility.
- **Risk-aware fan-out:** reviewer count scales with PR risk and size.
- **Resilient:** retries, usage-limit cooldowns, and crash-safe publication backed by SQLite.
- **Locked down:** runs as a dedicated systemd user, with systemd credentials and read-only worktrees.

Behavior, invariants, and rationale live in [DESIGN.md](DESIGN.md).

---

## Contents

- [Install](#install)
- [Setup](#setup)
- [Configuration](#configuration)
- [Output modes](#output-modes)
- [Operations](#operations)
- [Private MCP diagnostics](#private-mcp-diagnostics)
- [Development](#development)

---

## Install

Scout ships as an RPM for Rocky Linux / Enterprise Linux 9 and 10.

```bash
# Build dependencies (CRB required; omit python3-tomli on EL10)
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --set-enabled crb
sudo dnf install -y rpm-build python3-devel pyproject-rpm-macros python3-wheel \
  python3-setuptools python3-tomli systemd-rpm-macros \
  git tar gzip openssh-clients shadow-utils systemd

# Build and install
mkdir -p ~/rpmbuild/SOURCES
git archive --format=tar.gz --prefix=scout-0.1.0/ \
  -o ~/rpmbuild/SOURCES/scout-0.1.0.tar.gz HEAD
rpmbuild -ba packaging/scout.spec
sudo dnf install -y ~/rpmbuild/RPMS/noarch/scout-0.1.0-4.el9.noarch.rpm
```

The package installs:

| Path | Purpose |
| --- | --- |
| `/usr/bin/scout` | The daemon and CLI |
| `/usr/bin/scout-setup` | First-time setup helper |
| `/etc/scout/config.toml` | Configuration (kept on upgrade) |
| `/etc/scout/review.schema.json` | Bundled review schema (replaced on upgrade) |
| `/usr/lib/systemd/system/scout.service` | systemd unit |

## Setup

`scout-setup` creates `/etc/scout`, `/var/lib/scout`, and `/var/log/scout`,
installs Bitbucket credentials as systemd credentials, and adds the repository
to the config. From a source checkout, use `scripts/setup.sh` with the same flags.

```bash
sudo scout-setup \
  --bitbucket-url https://bitbucket.org/my-workspace/my-repo/pull-requests/ \
  --bitbucket-username-file ./bitbucket_username \
  --bitbucket-api-key-file ./bitbucket_api_key
```

| Flag | Effect |
| --- | --- |
| `--bitbucket-url` | Repository or PR URL. Run setup again with another URL to add more repositories. |
| `--bitbucket-oauth-client-id-file`<br>`--bitbucket-oauth-client-secret-file` | Use OAuth client credentials instead of username/API key. |
| `--bitbucket-ssh-key-file` | Install an existing SSH deploy key. Without one (dedicated-user mode), setup generates a key and prints the public key to add as a read-only access key. |
| `--logged-in-cli-current-user` | Run the service as your login user so it can reuse your existing Codex/Claude login. This is less isolated than the default dedicated `scout` user. |

Setup records absolute paths to the `codex` and `claude` binaries when it finds them.
If it doesn't, set `agents.<provider>.command` yourself, because systemd's `PATH` is minimal.

Then review the config and start the service:

```bash
sudo $EDITOR /etc/scout/config.toml
scout --config /etc/scout/config.toml --check-config
sudo systemctl enable --now scout
```

> [!NOTE]
> The packaged unit loads Basic Bitbucket credentials. If you configure OAuth or
> provider `auth_mode = "api"` by hand without `scout-setup`, add a drop-in:
>
> ```ini
> [Service]
> LoadCredential=
> LoadCredential=bitbucket_oauth_client_id:/etc/scout/secrets/bitbucket_oauth_client_id
> LoadCredential=bitbucket_oauth_client_secret:/etc/scout/secrets/bitbucket_oauth_client_secret
> LoadCredential=claude:/etc/scout/secrets/claude
> ```

## Configuration

See [`config/config.toml.example`](config/config.toml.example) for every option.

### Repositories

```toml
[[bitbucket.repositories]]
slug = "app"
clone_url = "git@bitbucket.org:my-workspace/app.git"
related_repositories = ["contracts"]       # read-only context for reviews
ignored_source_branches = ["^release/"]    # regexes
ignore_draft_pull_requests = true
# pr_ids = [123]                           # limit to specific PRs (testing)

[[bitbucket.repositories]]
slug = "contracts"
clone_url = "git@bitbucket.org:my-workspace/contracts.git"
review_enabled = false                     # context only, not polled
context_ref = "master"                     # defaults to the remote default branch
```

> [!WARNING]
> Scout sends the contents of related repositories to the AI provider, and
> excerpts can show up in PR comments. Only relate a repository if everyone who
> can read the primary repository's PRs may see its contents.
>
> Related repositories don't affect review identity. Bump
> `review.policy_version` after you change them if open PRs should be reviewed
> again.

### Providers

```toml
[agents]
providers = ["codex", "claude"]   # or the legacy single selector: strategy = "codex"

[agents.codex]
command = "codex"
model = "gpt-6.1-sol"
reasoning_effort = "medium"
fast_mode = true

[agents.claude]
enabled = true                    # Claude is disabled by default
auth_mode = "logged_in"           # or "api" (uses the `claude` systemd credential)
command = "claude"
model = "claude-opus-5-5"       # empty = CLI default
effort = "medium"
```

Each provider runs as its own job, and Scout never swaps one provider for
another. Claude is limited to read-only tools (`Task`, `Read`, `Grep`, `Glob`).
When a provider reports a usage-limit lockout, Scout pauses that provider for
five hours.

### Risk and reviewer sizing

Before each review, a small model rates the PR description's risk (failures
count as `medium`). Scout then picks the number of reviewers per lens:

| Risk | Reviewers per lens |
| --- | --- |
| `low` | 1 |
| `medium` | By changed LOC: 1 (≤150), 2 (≤600), 3 (≤1500), 4 (above) |
| `high` | Medium sizing plus `subagent_high_risk_bonus` |

The result is capped by `subagent_max_per_lens` and by `max_subagents / 6`. Each
provider needs `max_subagents` of at least 6. Codex allows 3 reviewers per lens
by default and Claude allows 1. You can override any sizing key under
`[agents.<provider>]`.

```toml
[review.risk]
provider = "codex"
model = "gpt-6.1-sol"
effort = "medium"
timeout_seconds = 120
```

## Output modes

### Code Insights reports (default)

Each provider publishes its own report (`scout-codex-v1`, `scout-claude-v1`) with
annotations on changed lines. Findings on removed (`OLD`) lines can't be shown
as annotations, so Scout drops them, except dead-code findings, which it posts
as PR comments instead.

```toml
[comments]
severities = ["CRITICAL"]   # also post these as native PR comments; [] disables
```

### Inline comments

```toml
[review]
output_mode = "inline_comments"

[review.request_comments]   # interprets "@scout" requests; also runs deduplication
provider = "codex"
model = "gpt-6.1-sol"
effort = "medium"

[review.deduplication]
enabled = true

[bitbucket]
bot_account_id = "{your-bot-account-uuid}"   # required for multi-provider
```

How inline mode works:

1. Every provider reviews the same frozen PR snapshot.
2. Once all providers finish, a selector merges overlapping findings. A finding
   that fully covers another replaces it, and partial overlaps are all kept.
3. Comments go out on changed `NEW` or `OLD` lines regardless of severity.
   Scout skips findings that an earlier Scout comment already reported.
4. If new commits arrive mid-review, findings are posted as regular PR comments
   that name the original commit.
5. Scout doesn't re-review on push. Mention `@scout` in a PR comment to request
   another round.

If a provider fails or stays in cooldown past
`queue.max_provider_recovery_seconds`, Scout publishes the results from the
providers that succeeded and lists the missing ones on the PR. If the selector
fails, Scout posts every finding without deduplication.

Scout feeds existing PR comments to reviewers as evidence. It suppresses a
repeated finding only when a developer's reply explicitly marks that issue out of
scope.

Matching and recovery rules are covered in
[docs/multi-provider-inline-review-design.md](docs/multi-provider-inline-review-design.md).

## Operations

### Upgrading

```bash
sudo systemctl stop scout
# Back up service.state_db (plus its -wal/-shm files) and /etc/scout/config.toml
sudo dnf upgrade -y /path/to/scout-<version>.rpm
sudo systemctl start scout
```

On startup, Scout migrates the database in place and puts interrupted jobs back
in the queue. Completed reviews stay completed, so an upgrade doesn't trigger new
reviews. You don't need to run `scout-setup` again.

> [!CAUTION]
> Never use `--reset-state-db` in production. It deletes all review history.

### CLI reference

| Command | Purpose |
| --- | --- |
| `scout --check-config` | Validate config without contacting Bitbucket or providers |
| `scout --once` | Run a single poll/review pass |
| `scout --usage-summary [--repo R] [--pr N]` | Summarize provider token and cost usage |
| `scout --list-unresolved-publications` | List inline comments whose delivery is uncertain |
| `scout --retry-publication ROUND_ID` | Retry delivery without rerunning providers |
| `scout --resolve-publication ID --expected-version V --outcome published --comment-id N` | Record that a comment did post |
| `scout --resolve-publication ID --expected-version V --outcome absent` | Record that a comment did not post |

Every command takes `--config /etc/scout/config.toml`.

### Logs and retention

| File | Contents |
| --- | --- |
| `state_dir/review-log.jsonl` | One audit record per validated review: provider, PR, commit, counts, and token usage |
| `state_dir/provider-usage.jsonl` | One record per provider attempt, written even if the review later fails |
| `state_dir/runs/` | Raw provider stdout and stderr |

These files are kept for `service.retention_days` (default 7, maximum 7). Saved
review snapshots stay in SQLite until their job is removed, which lets delayed
publication retries reuse the original review. Scout prunes state for closed PRs
on later polls.

## Private MCP diagnostics

The optional `scout-mcp.service` exposes read-only diagnostics to Codex and
Claude Code running on company laptops. Users add one internal HTTP URL; there
are no Scout accounts, tokens, certificates, or browser sign-in steps.

The company VPN and configured source networks control access. Every permitted
client can read retained logs, including private source-code excerpts. HTTP on
the internal segment between the VPN gateway and Scout is unencrypted. The
endpoint must remain inside this company-network boundary.

### Operator setup

Install the matching `scout` and `scout-mcp-runtime` RPMs on Rocky Linux 10
(x86_64). The optional runtime contains the pinned MCP dependencies; the ordinary Scout
daemon does not need them. Install `firewalld` and run it before applying MCP
configuration.

Set the following in `/etc/scout/config.toml`, replacing these example values:

```toml
[mcp]
enabled = true
bind_address = "10.20.30.40"
port = 8765
hostname = "scout.company.internal"
allowed_networks = ["10.100.0.0/16"]
```

The bind address must belong to the host's private IPv4 interface. The hostname
must resolve to it from company laptops. Use the client source ranges Scout
actually sees after any VPN gateway or NAT translation. IPv6 listening is not
enabled. See the example config for bounded-read limits and the diagnostic log
location; database and run-output locations come from `[service]`.
Diagnostic paths must be outside `/home`, `/root`, and `/run/user` so the MCP
service can keep its home-directory sandbox in place. Use a dedicated directory
for `mcp.log_path`.

```bash
sudo scout-setup --apply-mcp --config /etc/scout/config.toml
# First enable only: activate Scout's diagnostic logging and reader access.
sudo systemctl restart scout
```

Setup configures the dedicated MCP service account, file access, and its own
firewall rules, and prints client setup commands. It does not restart Scout.
The MCP service starts independently, so it remains available when Scout is
stopped or its provider configuration is broken.

### Connect a laptop

Use the URL printed by setup. For the example above:

```bash
codex mcp add scout --url http://scout.company.internal:8765/mcp
claude mcp add --transport http --scope user scout http://scout.company.internal:8765/mcp
```

Ask the agent to inspect Scout's service state, failed jobs, publication
blockers, logs, retained provider output, or token usage. The six tools are
`get_status`, `list_jobs`, `get_job`, `read_logs`, `read_run_output`, and
`get_usage`. They cannot retry jobs, restart services, execute commands, or
read arbitrary files.

`read_logs(source="daemon")` returns the current file and lists available
daily rotations. Pass a returned date as `rotation="YYYY-MM-DD"` to read that
retained file. Rotations use UTC dates and are limited by Scout's retention
setting, up to seven days. Review-audit and provider-usage logs use
`source="review"` and `source="usage"`.

Results report truncation and unavailable sources. Raw provider files are the
latest retained artifacts for a job and can be overwritten by a retry. A
validated review record does not prove that publication succeeded. When Scout
stops, SQLite may remove the WAL files needed for read-only access; MCP then
reports database state as unavailable while service status and retained logs
remain accessible. Failures before Scout's Python entrypoint starts remain in
the local system journal.

`max_bytes` accepts 2048–65536 bytes. The HTTP service additionally limits the
diagnostic JSON payload to 24 KiB so the complete MCP response stays below
64 KiB after JSON escaping and protocol metadata. Follow returned cursors to
read additional records; usage pages explicitly report partial subtotals.

### Disable or change the endpoint

Edit `[mcp]` and re-run `scout-setup --apply-mcp`. Setting `enabled = false`
stops and disables MCP and removes its managed firewall rules without stopping
Scout. Editing TOML alone does not reconfigure an already-running service.
MCP is disabled by default; missing MCP dependencies or invalid MCP settings do
not prevent the ordinary Scout daemon from running.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m scout --config config/config.toml.example --check-config
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

Validate the optional MCP runtime and build its Rocky 10 RPMs with Docker:

```bash
scripts/test-rocky10.sh
docker build --target rpm-export \
  --output type=local,dest=build/rocky10-rpms \
  -f packaging/Dockerfile.rocky10 .
```

The build downloads hash-locked wheels in a dedicated image stage, then builds
both RPMs without network access. The installed runtime uses those packaged
dependencies; setup does not install Python packages on the host. The test
container exercises HTTP, file permissions, and RPM installation. A real
deployment still needs checks from allowed and denied network sources and on
a Rocky 10 host with SELinux enforcing.

Before rollout, use both client commands above from a VPN laptop and call
`get_status` and `read_logs`. Confirm that a client outside the allowed source
networks cannot connect. On the deployment host, check `systemctl status
scout-mcp`, `journalctl -u scout-mcp`, and `getenforce`; review any SELinux
denials without disabling enforcement. Stop MCP and confirm Scout continues
reviewing, then start it again. Finally apply `enabled = false` and confirm the
listener closes while Scout remains active.

## License

Apache License 2.0. See [LICENSE](LICENSE).
