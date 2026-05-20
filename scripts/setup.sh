#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SCOUT_SERVICE_NAME:-scout}"
SERVICE_USER="scout"
SERVICE_GROUP="scout"
CONFIG_DIR="${SCOUT_CONFIG_DIR:-/etc/scout}"
SECRET_DIR="${SCOUT_SECRET_DIR:-${CONFIG_DIR}/secrets}"
STATE_DIR="${SCOUT_STATE_DIR:-/var/lib/scout}"
LOG_DIR="${SCOUT_LOG_DIR:-/var/log/scout}"
CONFIG_PATH="${SCOUT_CONFIG_PATH:-${CONFIG_DIR}/config.toml}"
SCHEMA_PATH="${SCOUT_SCHEMA_PATH:-${CONFIG_DIR}/review.schema.json}"
SERVICE_PATH="${SCOUT_SERVICE_PATH:-/etc/systemd/system/${SERVICE_NAME}.service}"
USE_CURRENT_USER=0
SERVICE_USER_SET=0
SERVICE_GROUP_SET=0
ENABLE_NOW=0
PRINT_UNIT=0
BITBUCKET_USERNAME_FILE=""
BITBUCKET_API_KEY_FILE=""
BITBUCKET_SSH_KEY_FILE=""
BITBUCKET_URL=""
LOAD_SSH_CREDENTIAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_SRC="${ROOT_DIR}/config/config.toml.example"
SCHEMA_SRC="${ROOT_DIR}/config/review.schema.json"
BINARY_PATH="$(command -v scout || true)"
if [[ -z "${BINARY_PATH}" ]]; then
  BINARY_PATH="/usr/bin/scout"
fi

usage() {
  cat <<'USAGE'
Usage: scout-setup [options]

Configure Scout as a systemd service. By default the service runs as the
dedicated scout user. Use --logged-in-cli-current-user only when the selected
agent CLI must reuse the invoking user's existing login.

Options:
  --logged-in-cli-current-user  Run the service as the invoking non-root user.
  --service-user USER           Dedicated service user to create/use.
  --service-group GROUP         Dedicated service group to create/use.
  --binary PATH                 Absolute scout binary path.
  --config PATH                 Service config path.
  --bitbucket-url URL           Bitbucket Cloud repo or pull-requests URL.
  --bitbucket-username-file PATH
  --bitbucket-api-key-file PATH
  --bitbucket-ssh-key-file PATH Optional SSH key credential source.
  --enable-now                  Enable and start the service after setup.
  --print-unit                  Print the generated unit and exit.
  -h, --help                    Show this help.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "${value}" || "${value}" == --* ]]; then
    die "${option} requires a value"
  fi
}

require_absolute_path() {
  local option="$1"
  local value="$2"
  case "${value}" in
    /*) ;;
    *) die "${option} must be an absolute path" ;;
  esac
}

invoking_user() {
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    echo "${SUDO_USER}"
  else
    id -un
  fi
}

primary_group_for_user() {
  id -gn "$1"
}

home_for_user() {
  getent passwd "$1" | cut -d: -f6
}

service_home_dir() {
  if [[ "${USE_CURRENT_USER}" -eq 1 ]]; then
    home_for_user "${SERVICE_USER}"
  else
    echo "${STATE_DIR}"
  fi
}

absolute_command_path() {
  local command_name="$1"
  local command_path
  local command_output
  command_path="$(type -P "${command_name}" || true)"
  if ! is_executable_path "${command_path}"; then
    command_path=""
  fi
  if [[ -z "${command_path}" && "${USE_CURRENT_USER}" -eq 1 ]]; then
    if command -v runuser >/dev/null 2>&1; then
      command_output="$(
        runuser -u "${SERVICE_USER}" -- bash -c 'command -v "$1"' bash "${command_name}" 2>/dev/null || true
      )"
      command_path="$(first_executable_path "${command_output}" || true)"
    fi
    if [[ -z "${command_path}" ]]; then
      command_path="$(common_user_command_path "${command_name}" || true)"
    fi
  fi
  if [[ -z "${command_path}" ]]; then
    return 1
  fi
  echo "${command_path}"
}

is_executable_path() {
  local path="${1:-}"
  [[ "${path}" == /* && -f "${path}" && -x "${path}" ]]
}

first_executable_path() {
  local output="$1"
  local line
  while IFS= read -r line; do
    line="${line%$'\r'}"
    if is_executable_path "${line}"; then
      echo "${line}"
      return 0
    fi
  done <<<"${output}"
  return 1
}

common_user_command_path() {
  local command_name="$1"
  local home_dir
  local dir
  local candidate
  home_dir="$(service_home_dir)"
  for dir in \
    "${home_dir}/.local/bin" \
    "${home_dir}/bin" \
    "/home/linuxbrew/.linuxbrew/bin" \
    "/usr/local/bin" \
    "/usr/bin" \
    "/bin"
  do
    candidate="${dir}/${command_name}"
    if is_executable_path "${candidate}"; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

toml_set_value_in_table() {
  local path="$1"
  local table="$2"
  local key="$3"
  local value="$4"
  local string_value="$5"
  local tmp
  if [[ ! -f "${path}" ]]; then
    return
  fi
  tmp="$(mktemp)"
  awk -v table="[${table}]" -v key="${key}" -v value="${value}" -v string_value="${string_value}" '
    function escaped_string(raw) {
      gsub(/\\/, "\\\\", raw)
      gsub(/"/, "\\\"", raw)
      return raw
    }
    function rendered_value() {
      if (string_value == "1") {
        return "\"" escaped_string(value) "\""
      }
      return value
    }
    function setting_line() {
      return key " = " rendered_value()
    }
    skip_multiline {
      if ($0 ~ /"[[:space:]]*(#.*)?$/) {
        skip_multiline = 0
      }
      next
    }
    /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
      if (in_table && !done) {
        print setting_line()
        done = 1
      }
      in_table = ($0 == table)
      print
      next
    }
    in_table && $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      old_value = $0
      sub(/^[^=]*=/, "", old_value)
      if (old_value ~ /^[[:space:]]*"/ && old_value !~ /"[[:space:]]*(#.*)?$/) {
        skip_multiline = 1
      }
      print setting_line()
      done = 1
      next
    }
    { print }
    END {
      if (in_table && !done) {
        print setting_line()
      }
    }
  ' "${path}" >"${tmp}"
  install -m 0644 "${tmp}" "${path}"
  rm -f "${tmp}"
}

toml_set_string_in_table() {
  toml_set_value_in_table "$1" "$2" "$3" "$4" 1
}

toml_set_integer_in_table() {
  toml_set_value_in_table "$1" "$2" "$3" "$4" 0
}

parse_bitbucket_url() {
  local url="$1"
  local stripped
  local path
  case "${url}" in
    https://bitbucket.org/*)
      stripped="${url#https://bitbucket.org/}"
      ;;
    http://bitbucket.org/*)
      stripped="${url#http://bitbucket.org/}"
      ;;
    git@bitbucket.org:*)
      stripped="${url#git@bitbucket.org:}"
      ;;
    *)
      die "--bitbucket-url must be a Bitbucket Cloud URL under bitbucket.org"
      ;;
  esac
  stripped="${stripped%.git}"
  path="${stripped%%\?*}"
  path="${path%%#*}"
  path="${path#/}"
  IFS=/ read -r DERIVED_WORKSPACE DERIVED_REPO DERIVED_REST DERIVED_PR_ID _ <<<"${path}"
  DERIVED_REPO="${DERIVED_REPO%.git}"
  if [[ -z "${DERIVED_WORKSPACE:-}" || -z "${DERIVED_REPO:-}" ]]; then
    die "could not derive workspace and repository from --bitbucket-url ${url}"
  fi
  if [[ "${DERIVED_REST:-}" != "pull-requests" ]]; then
    DERIVED_PR_ID=""
  elif [[ ! "${DERIVED_PR_ID:-}" =~ ^[0-9]+$ ]]; then
    DERIVED_PR_ID=""
  fi
}

ssh_clone_url_for_repo() {
  echo "git@bitbucket.org:${DERIVED_WORKSPACE}/${DERIVED_REPO}.git"
}

write_initial_config_for_bitbucket_url() {
  local clone_url
  local tmp
  clone_url="$(ssh_clone_url_for_repo)"
  tmp="$(mktemp)"
  cat >"${tmp}" <<CONFIG
[service]
worker_id = "reviewer-1"
state_db = "${STATE_DIR}/state.db"
state_dir = "${STATE_DIR}"
log_level = "INFO"
retention_days = 7

[bitbucket]
workspace = "${DERIVED_WORKSPACE}"
api_base_url = "https://api.bitbucket.org/2.0"
api_auth = "basic"
api_username_credential = "bitbucket_username"
api_key_credential = "bitbucket_api_key"

[[bitbucket.repositories]]
slug = "${DERIVED_REPO}"
clone_url = "${clone_url}"

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

[review]
policy_version = "v1"
schema_path = "${SCHEMA_PATH}"
max_findings = 100
subagent_small_loc_limit = 150
subagent_medium_loc_limit = 600
subagent_large_loc_limit = 1500
subagent_high_risk_bonus = 1
subagent_max_per_lens = 4

[review.risk]
enabled = true
provider = "codex"
model = "gpt-5.4"
effort = "low"
timeout_seconds = 120

[agents]
strategy = "codex"
# providers = ["codex", "claude"]

[agents.codex]
enabled = true
auth_mode = "logged_in"
credential = "codex"
home_dir = "${STATE_DIR}/agents/codex/main"
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
enabled = true
auth_mode = "logged_in"
credential = "claude"
home_dir = "${STATE_DIR}/agents/claude/main"
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

[reports.codex]

[reports.claude]
CONFIG
  install -m 0644 "${tmp}" "${CONFIG_PATH}"
  rm -f "${tmp}"
  echo "Wrote ${CONFIG_PATH} for Bitbucket repository ${DERIVED_WORKSPACE}/${DERIVED_REPO}."
}

append_bitbucket_repo_if_missing() {
  local clone_url
  local existing_workspace
  clone_url="$(ssh_clone_url_for_repo)"
  existing_workspace="$(awk -F= '
    /^[[:space:]]*\[bitbucket\][[:space:]]*$/ { in_bitbucket=1; next }
    /^[[:space:]]*\[/ { in_bitbucket=0 }
    in_bitbucket && /^[[:space:]]*workspace[[:space:]]*=/ {
      value=$2
      sub(/[[:space:]]*#.*/, "", value)
      gsub(/^[[:space:]]*"/, "", value)
      gsub(/"[[:space:]]*$/, "", value)
      print value
      exit
    }
  ' "${CONFIG_PATH}")"
  if [[ -n "${existing_workspace}" && "${existing_workspace}" != "my-workspace" && "${existing_workspace}" != "${DERIVED_WORKSPACE}" ]]; then
    die "${CONFIG_PATH} already targets Bitbucket workspace ${existing_workspace}; use a separate config for ${DERIVED_WORKSPACE}"
  fi
  if grep -Eq '^[[:space:]]*slug[[:space:]]*=[[:space:]]*"repo-a"[[:space:]]*$' "${CONFIG_PATH}" \
    || grep -Eq '^[[:space:]]*slug[[:space:]]*=[[:space:]]*"repo-b"[[:space:]]*$' "${CONFIG_PATH}"; then
    write_initial_config_for_bitbucket_url
    return
  fi
  toml_set_string_in_table "${CONFIG_PATH}" "bitbucket" "workspace" "${DERIVED_WORKSPACE}"
  if grep -Eq '^[[:space:]]*slug[[:space:]]*=[[:space:]]*"'"${DERIVED_REPO}"'"[[:space:]]*$' "${CONFIG_PATH}"; then
    echo "Bitbucket repository ${DERIVED_WORKSPACE}/${DERIVED_REPO} is already present in ${CONFIG_PATH}."
    return
  fi
  cat >>"${CONFIG_PATH}" <<CONFIG

[[bitbucket.repositories]]
slug = "${DERIVED_REPO}"
clone_url = "${clone_url}"
CONFIG
  echo "Added Bitbucket repository ${DERIVED_WORKSPACE}/${DERIVED_REPO} to ${CONFIG_PATH}."
}

write_detected_provider_commands() {
  local codex_command
  local claude_command
  if codex_command="$(absolute_command_path codex)"; then
    toml_set_string_in_table "${CONFIG_PATH}" "agents.codex" "command" "${codex_command}"
    echo "Detected codex CLI at ${codex_command}; wrote agents.codex.command."
  fi
  if claude_command="$(absolute_command_path claude)"; then
    toml_set_string_in_table "${CONFIG_PATH}" "agents.claude" "command" "${claude_command}"
    echo "Detected claude CLI at ${claude_command}; wrote agents.claude.command."
  fi
}

codex_max_subagents_from_config() {
  local codex_config="$1"
  if [[ ! -r "${codex_config}" ]]; then
    return
  fi
  awk -F= '
    /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
      in_agents = ($0 ~ /^[[:space:]]*\[agents\][[:space:]]*$/)
    }
    /^[[:space:]]*max_subagents[[:space:]]*=/ {
      value = $2
      sub(/[[:space:]]*#.*/, "", value)
      gsub(/[[:space:]_"]/, "", value)
      if (value ~ /^[0-9]+$/) {
        print value
      }
      exit
    }
    in_agents && /^[[:space:]]*max_threads[[:space:]]*=/ {
      value = $2
      sub(/[[:space:]]*#.*/, "", value)
      gsub(/[[:space:]_"]/, "", value)
      if (value ~ /^[0-9]+$/) {
        print value
      }
      exit
    }
  ' "${codex_config}"
}

write_detected_codex_max_subagents() {
  local home_dir
  local codex_config
  local max_subagents
  local scout_max_subagents
  local max_per_lens
  home_dir="$(service_home_dir)"
  if [[ -z "${home_dir}" ]]; then
    return
  fi
  codex_config="${home_dir}/.codex/config.toml"
  max_subagents="$(codex_max_subagents_from_config "${codex_config}")"
  if [[ -z "${max_subagents}" ]]; then
    return
  fi
  scout_max_subagents="${max_subagents}"
  if (( scout_max_subagents > 15 )); then
    scout_max_subagents=15
  fi
  toml_set_integer_in_table "${CONFIG_PATH}" "agents.codex" "max_subagents" "${scout_max_subagents}"
  max_per_lens=$(( scout_max_subagents / 5 ))
  if (( max_per_lens < 1 )); then
    max_per_lens=1
  elif (( max_per_lens > 3 )); then
    max_per_lens=3
  fi
  toml_set_integer_in_table "${CONFIG_PATH}" "agents.codex" "subagent_max_per_lens" "${max_per_lens}"
  echo "Detected Codex max_subagents=${max_subagents} from ${codex_config}; wrote agents.codex.max_subagents=${scout_max_subagents}."
  if (( max_subagents < 10 )); then
    echo "warning: Codex max_subagents is ${max_subagents}; set it to at least 10 for Scout reviewer fan-out." >&2
  fi
}

ensure_service_ssh_key() {
  if [[ "${USE_CURRENT_USER}" -eq 1 ]]; then
    return
  fi
  local home_dir
  local ssh_dir
  local key_path
  local key_comment
  local host_name
  home_dir="$(service_home_dir)"
  if [[ -z "${home_dir}" ]]; then
    die "could not determine home directory for ${SERVICE_USER}"
  fi
  ssh_dir="${home_dir}/.ssh"
  key_path="${ssh_dir}/id_ed25519"
  install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${ssh_dir}"
  host_name="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo scout)"
  key_comment="scout@${host_name}"
  if [[ ! -f "${key_path}" ]]; then
    if ! command -v ssh-keygen >/dev/null 2>&1; then
      echo "warning: ssh-keygen not found; create ${key_path} for Bitbucket SSH access before starting" >&2
      return
    fi
    ssh-keygen -q -t ed25519 -N "" -C "${key_comment}" -f "${key_path}"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${key_path}" "${key_path}.pub"
    chmod 0600 "${key_path}"
    chmod 0644 "${key_path}.pub"
    echo "Created SSH keypair for ${SERVICE_USER} at ${key_path}."
  elif [[ ! -f "${key_path}.pub" ]]; then
    if ! command -v ssh-keygen >/dev/null 2>&1; then
      echo "warning: ${key_path}.pub is missing and ssh-keygen was not found" >&2
      return
    fi
    ssh-keygen -y -f "${key_path}" >"${key_path}.pub"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${key_path}.pub"
    chmod 0644 "${key_path}.pub"
  fi
  if [[ -f "${key_path}" ]]; then
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${key_path}"
    chmod 0600 "${key_path}"
  fi
  if [[ -f "${key_path}.pub" ]]; then
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${key_path}.pub"
    chmod 0644 "${key_path}.pub"
  fi
  if [[ -f "${key_path}.pub" ]]; then
    echo
    echo "Scout SSH public key for Bitbucket read access:"
    sed 's/^/  /' "${key_path}.pub"
    echo "Add this public key to Bitbucket as a repository or workspace access key with read access."
    echo "Use SSH clone URLs in ${CONFIG_PATH}; no bitbucket_ssh_key credential is required for this key."
    echo
  fi
}

while (($#)); do
  case "$1" in
    --logged-in-cli-current-user)
      USE_CURRENT_USER=1
      shift
      ;;
    --service-user)
      require_value "$1" "${2:-}"
      SERVICE_USER="$2"
      SERVICE_USER_SET=1
      shift 2
      ;;
    --service-group)
      require_value "$1" "${2:-}"
      SERVICE_GROUP="$2"
      SERVICE_GROUP_SET=1
      shift 2
      ;;
    --binary)
      require_value "$1" "${2:-}"
      BINARY_PATH="$2"
      shift 2
      ;;
    --config)
      require_value "$1" "${2:-}"
      CONFIG_PATH="$2"
      shift 2
      ;;
    --bitbucket-url)
      require_value "$1" "${2:-}"
      BITBUCKET_URL="$2"
      shift 2
      ;;
    --bitbucket-username-file)
      require_value "$1" "${2:-}"
      BITBUCKET_USERNAME_FILE="$2"
      shift 2
      ;;
    --bitbucket-api-key-file)
      require_value "$1" "${2:-}"
      BITBUCKET_API_KEY_FILE="$2"
      shift 2
      ;;
    --bitbucket-ssh-key-file)
      require_value "$1" "${2:-}"
      BITBUCKET_SSH_KEY_FILE="$2"
      LOAD_SSH_CREDENTIAL=1
      shift 2
      ;;
    --enable-now)
      ENABLE_NOW=1
      shift
      ;;
    --print-unit)
      PRINT_UNIT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

if [[ "${USE_CURRENT_USER}" -eq 1 ]]; then
  if [[ "${SERVICE_USER_SET}" -eq 1 || "${SERVICE_GROUP_SET}" -eq 1 ]]; then
    die "--logged-in-cli-current-user cannot be combined with --service-user or --service-group"
  fi
  SERVICE_USER="$(invoking_user)"
  if [[ "${SERVICE_USER}" == "root" ]]; then
    die "--logged-in-cli-current-user requires sudo from a non-root login user"
  fi
  SERVICE_GROUP="$(primary_group_for_user "${SERVICE_USER}")"
fi

require_absolute_path "--binary" "${BINARY_PATH}"
require_absolute_path "--config" "${CONFIG_PATH}"

if [[ -n "${BITBUCKET_URL}" ]]; then
  parse_bitbucket_url "${BITBUCKET_URL}"
fi

if [[ -f "${SECRET_DIR}/bitbucket_ssh_key" ]]; then
  LOAD_SSH_CREDENTIAL=1
fi

render_unit() {
  local protect_home="true"
  local home_dir
  local read_write_paths="${STATE_DIR} ${LOG_DIR}"
  home_dir="$(service_home_dir)"
  if [[ -z "${home_dir}" ]]; then
    die "could not determine home directory for ${SERVICE_USER}"
  fi
  if [[ "${USE_CURRENT_USER}" -eq 1 ]]; then
    protect_home="false"
    read_write_paths="${STATE_DIR} ${LOG_DIR} ${home_dir}"
  fi

  cat <<UNIT
[Unit]
Description=Scout Bitbucket PR AI Review Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
Environment=HOME=${home_dir}
ExecStartPre=${BINARY_PATH} --config ${CONFIG_PATH} --check-startup
ExecStart=${BINARY_PATH} --config ${CONFIG_PATH}
ExecStopPost=-${BINARY_PATH} --config ${CONFIG_PATH} --recover-abandoned-jobs
Restart=on-failure
RestartSec=10

StateDirectory=scout
ConfigurationDirectory=scout
LogsDirectory=scout

LoadCredential=bitbucket_username:${SECRET_DIR}/bitbucket_username
LoadCredential=bitbucket_api_key:${SECRET_DIR}/bitbucket_api_key
UNIT
  if [[ "${LOAD_SSH_CREDENTIAL}" -eq 1 ]]; then
    echo "LoadCredential=bitbucket_ssh_key:${SECRET_DIR}/bitbucket_ssh_key"
  else
    echo "# Optional: add LoadCredential=bitbucket_ssh_key:${SECRET_DIR}/bitbucket_ssh_key when using a deploy key."
  fi
  cat <<UNIT

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=${protect_home}
ReadWritePaths=${read_write_paths}

[Install]
WantedBy=multi-user.target
UNIT
}

if [[ "${PRINT_UNIT}" -eq 1 ]]; then
  render_unit
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  die "run with sudo or as root"
fi

ensure_service_identity() {
  if [[ "${USE_CURRENT_USER}" -eq 1 ]]; then
    return
  fi
  if ! getent group "${SERVICE_GROUP}" >/dev/null; then
    groupadd --system "${SERVICE_GROUP}"
  fi
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --gid "${SERVICE_GROUP}" --home-dir "${STATE_DIR}" \
      --shell /sbin/nologin --comment "Scout PR review daemon" "${SERVICE_USER}"
  fi
}

install_secret() {
  local name="$1"
  local source_file="$2"
  local option="$3"
  local dest="${SECRET_DIR}/${name}"
  if [[ -n "${source_file}" ]]; then
    install -m 0600 -o root -g root "${source_file}" "${dest}"
  elif [[ ! -f "${dest}" ]]; then
    echo "warning: ${dest} does not exist; create it or rerun with ${option} before starting" >&2
  fi
}

ensure_service_identity
install -d -m 0755 "$(dirname "${CONFIG_PATH}")"
install -d -m 0750 "${SECRET_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${STATE_DIR}" "${LOG_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${STATE_DIR}" "${LOG_DIR}"

if [[ ! -f "${CONFIG_PATH}" && -n "${BITBUCKET_URL}" ]]; then
  write_initial_config_for_bitbucket_url
elif [[ ! -f "${CONFIG_PATH}" ]]; then
  if [[ -f "${CONFIG_SRC}" ]]; then
    install -m 0644 "${CONFIG_SRC}" "${CONFIG_PATH}"
  else
    echo "warning: ${CONFIG_PATH} does not exist and ${CONFIG_SRC} was not found" >&2
  fi
elif [[ -n "${BITBUCKET_URL}" ]]; then
  append_bitbucket_repo_if_missing
fi
if [[ ! -f "${SCHEMA_PATH}" ]]; then
  if [[ -f "${SCHEMA_SRC}" ]]; then
    install -m 0644 "${SCHEMA_SRC}" "${SCHEMA_PATH}"
  else
    echo "warning: ${SCHEMA_PATH} does not exist and ${SCHEMA_SRC} was not found" >&2
  fi
fi
write_detected_provider_commands
write_detected_codex_max_subagents

install_secret "bitbucket_username" "${BITBUCKET_USERNAME_FILE}" "--bitbucket-username-file"
install_secret "bitbucket_api_key" "${BITBUCKET_API_KEY_FILE}" "--bitbucket-api-key-file"
if [[ -n "${BITBUCKET_SSH_KEY_FILE}" ]]; then
  install_secret "bitbucket_ssh_key" "${BITBUCKET_SSH_KEY_FILE}" "--bitbucket-ssh-key-file"
  LOAD_SSH_CREDENTIAL=1
elif [[ -f "${SECRET_DIR}/bitbucket_ssh_key" ]]; then
  LOAD_SSH_CREDENTIAL=1
fi
ensure_service_ssh_key

tmp_unit="$(mktemp)"
trap 'rm -f "${tmp_unit}"' EXIT
render_unit >"${tmp_unit}"
install -m 0644 "${tmp_unit}" "${SERVICE_PATH}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  if [[ "${ENABLE_NOW}" -eq 1 ]]; then
    systemctl enable --now "${SERVICE_NAME}.service"
  else
    echo "Unit installed at ${SERVICE_PATH}. Edit ${CONFIG_PATH}, then run:"
    echo "  sudo systemctl enable --now ${SERVICE_NAME}.service"
  fi
else
  echo "warning: systemctl not found; unit installed at ${SERVICE_PATH}" >&2
fi
