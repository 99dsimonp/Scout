import os
import pwd
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "setup.sh"


def clean_env():
    env = os.environ.copy()
    env.pop("SUDO_USER", None)
    return env


def write_executable(path, content):
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


class SetupScriptTests(unittest.TestCase):
    def test_print_unit_defaults_to_dedicated_user_and_loadcredential_files(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")

        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / "config.toml")
            result = subprocess.run(
                [
                    "bash",
                    str(SETUP),
                    "--print-unit",
                    "--binary",
                    "/usr/bin/scout",
                    "--config",
                    config_path,
                ],
                check=True,
                env=clean_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertIn("User=scout\n", result.stdout)
        self.assertIn("Group=scout\n", result.stdout)
        self.assertIn(
            "ExecStopPost=-/usr/bin/scout --config {} --recover-abandoned-jobs".format(config_path),
            result.stdout,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/scout --config {} --check-startup".format(config_path),
            result.stdout,
        )
        self.assertIn("LoadCredential=bitbucket_username:/etc/scout/secrets/bitbucket_username", result.stdout)
        self.assertIn("LoadCredential=bitbucket_api_key:/etc/scout/secrets/bitbucket_api_key", result.stdout)
        self.assertTrue(
            "# Optional: add LoadCredential=bitbucket_ssh_key:" in result.stdout
            or "LoadCredential=bitbucket_ssh_key:/etc/scout/secrets/bitbucket_ssh_key" in result.stdout
        )
        self.assertIn("ProtectHome=true\n", result.stdout)

    def test_print_unit_can_load_oauth_bitbucket_credentials(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")

        result = subprocess.run(
            [
                "bash",
                str(SETUP),
                "--print-unit",
                "--binary",
                "/usr/bin/scout",
                "--config",
                "/etc/scout/config.toml",
                "--bitbucket-oauth-client-id-file",
                "/tmp/client-id",
                "--bitbucket-oauth-client-secret-file",
                "/tmp/client-secret",
            ],
            check=True,
            env=clean_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertIn(
            "LoadCredential=bitbucket_oauth_client_id:/etc/scout/secrets/bitbucket_oauth_client_id",
            result.stdout,
        )
        self.assertIn(
            "LoadCredential=bitbucket_oauth_client_secret:/etc/scout/secrets/bitbucket_oauth_client_secret",
            result.stdout,
        )
        self.assertNotIn("LoadCredential=bitbucket_username:", result.stdout)
        self.assertNotIn("LoadCredential=bitbucket_api_key:", result.stdout)

    def test_print_unit_current_user_mode_is_explicit(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")
        if os.geteuid() == 0:
            self.skipTest("current-user mode requires a non-root login user")

        result = subprocess.run(
            [
                "bash",
                str(SETUP),
                "--print-unit",
                "--logged-in-cli-current-user",
                "--binary",
                "/usr/bin/scout",
                "--config",
                "/etc/scout/config.toml",
            ],
            check=True,
            env=clean_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertIn("User={}\n".format(pwd.getpwuid(os.getuid()).pw_name), result.stdout)
        self.assertIn(
            "ExecStopPost=-/usr/bin/scout --config /etc/scout/config.toml --recover-abandoned-jobs",
            result.stdout,
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/scout --config /etc/scout/config.toml --check-startup",
            result.stdout,
        )
        self.assertIn("ProtectHome=false\n", result.stdout)

    def test_setup_writes_detected_provider_paths_codex_limit_and_dedicated_ssh_key(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            config_dir = tmp_path / "etc" / "scout"
            secret_dir = config_dir / "secrets"
            state_dir = tmp_path / "var" / "lib" / "scout"
            log_dir = tmp_path / "var" / "log" / "scout"
            service_path = tmp_path / "systemd" / "scout.service"
            schema_path = config_dir / "review.schema.json"
            config_path = config_dir / "config.toml"
            bin_dir.mkdir()
            config_dir.mkdir(parents=True)
            config_path.write_text(
                textwrap.dedent(
                    """
                    [bitbucket]
                    workspace = "my-workspace"

                    [[bitbucket.repositories]]
                    slug = "repo-a"
                    clone_url = "git@bitbucket.org:my-workspace/repo-a.git"

                    [[bitbucket.repositories]]
                    slug = "repo-b"
                    clone_url = "git@bitbucket.org:my-workspace/repo-b.git"
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (state_dir / ".codex").mkdir(parents=True)
            (state_dir / ".codex" / "config.toml").write_text("[agents]\nmax_threads = 8\n", encoding="utf-8")

            write_executable(
                bin_dir / "id",
                """
                #!/usr/bin/env bash
                if [[ "${1:-}" == "-u" ]]; then
                  echo 0
                  exit 0
                fi
                if [[ "${1:-}" == "-gn" ]]; then
                  echo scout
                  exit 0
                fi
                exec /usr/bin/id "$@"
                """,
            )
            write_executable(
                bin_dir / "getent",
                """
                #!/usr/bin/env bash
                if [[ "${1:-}" == "group" ]]; then
                  echo "scout:x:999:"
                  exit 0
                fi
                if [[ "${1:-}" == "passwd" ]]; then
                  echo "scout:x:999:999:Scout:__STATE_DIR__:/sbin/nologin"
                  exit 0
                fi
                exec /usr/bin/getent "$@"
                """.replace("__STATE_DIR__", str(state_dir)),
            )
            write_executable(
                bin_dir / "install",
                """
                #!/usr/bin/env bash
                set -euo pipefail
                mode=""
                make_dirs=0
                args=()
                while (($#)); do
                  case "$1" in
                    -d)
                      make_dirs=1
                      shift
                      ;;
                    -m)
                      mode="$2"
                      shift 2
                      ;;
                    -o|-g)
                      shift 2
                      ;;
                    *)
                      args+=("$1")
                      shift
                      ;;
                  esac
                done
                if [[ "${make_dirs}" -eq 1 ]]; then
                  for path in "${args[@]}"; do
                    mkdir -p "${path}"
                    if [[ -n "${mode}" ]]; then chmod "${mode}" "${path}"; fi
                  done
                else
                  src="${args[0]}"
                  dest="${args[1]}"
                  mkdir -p "$(dirname "${dest}")"
                  cp "${src}" "${dest}"
                  if [[ -n "${mode}" ]]; then chmod "${mode}" "${dest}"; fi
                fi
                """,
            )
            for command in ("groupadd", "useradd", "chown", "systemctl", "codex", "claude"):
                write_executable(
                    bin_dir / command,
                    """
                    #!/usr/bin/env bash
                    exit 0
                    """,
                )
            write_executable(
                bin_dir / "ssh-keygen",
                """
                #!/usr/bin/env bash
                key_path=""
                print_public=0
                while (($#)); do
                  case "$1" in
                    -f)
                      key_path="$2"
                      shift 2
                      ;;
                    -y)
                      print_public=1
                      shift
                      ;;
                    *)
                      shift
                      ;;
                  esac
                done
                if [[ "${print_public}" -eq 1 ]]; then
                  echo "ssh-ed25519 fake-key scout"
                  exit 0
                fi
                echo "PRIVATE" >"${key_path}"
                echo "ssh-ed25519 fake-key scout" >"${key_path}.pub"
                """,
            )

            env = clean_env()
            env.update(
                {
                    "PATH": "{}:{}".format(bin_dir, env["PATH"]),
                    "SCOUT_CONFIG_DIR": str(config_dir),
                    "SCOUT_SECRET_DIR": str(secret_dir),
                    "SCOUT_STATE_DIR": str(state_dir),
                    "SCOUT_LOG_DIR": str(log_dir),
                    "SCOUT_SERVICE_PATH": str(service_path),
                    "SCOUT_SCHEMA_PATH": str(schema_path),
                }
            )

            result = subprocess.run(
                [
                    "bash",
                    str(SETUP),
                    "--binary",
                    "/usr/bin/scout",
                    "--config",
                    str(config_path),
                    "--bitbucket-url",
                    "https://bitbucket.org/example-workspace/example-repo/pull-requests/1148/overview",
                ],
                check=True,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            config = config_path.read_text(encoding="utf-8")
            self.assertIn('workspace = "example-workspace"', config)
            self.assertIn('slug = "example-repo"', config)
            self.assertIn('clone_url = "git@bitbucket.org:example-workspace/example-repo.git"', config)
            self.assertNotIn('slug = "repo-a"', config)
            self.assertIn("interval_seconds = 600", config)
            self.assertIn("job_timeout_seconds = 1800", config)
            self.assertIn('command = "{}"'.format(bin_dir / "codex"), config)
            self.assertIn('command = "{}"'.format(bin_dir / "claude"), config)
            self.assertRegex(config, r"(?s)\[agents\.claude\].*enabled = false")
            self.assertIn("max_subagents = 8", config)
            self.assertRegex(
                config,
                r"(?s)\[agents\.codex\].*subagent_max_per_lens = 1.*\[agents\.claude\].*subagent_max_per_lens = 1",
            )
            self.assertTrue((state_dir / ".ssh" / "id_ed25519").exists())
            self.assertIn("ssh-ed25519 fake-key scout", result.stdout)
            self.assertIn("Add this public key to Bitbucket", result.stdout)
            self.assertIn("Codex max_subagents is 8", result.stderr)

    def test_setup_ignores_noisy_login_output_when_detecting_provider_paths(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            cli_dir = tmp_path / "cli"
            home_dir = tmp_path / "home"
            config_dir = tmp_path / "etc" / "scout"
            secret_dir = config_dir / "secrets"
            state_dir = tmp_path / "var" / "lib" / "scout"
            log_dir = tmp_path / "var" / "log" / "scout"
            service_path = tmp_path / "systemd" / "scout.service"
            schema_path = config_dir / "review.schema.json"
            config_path = config_dir / "config.toml"
            bin_dir.mkdir()
            cli_dir.mkdir()
            (home_dir / ".codex").mkdir(parents=True)
            (home_dir / ".codex" / "config.toml").write_text("[agents]\nmax_threads = 20\n", encoding="utf-8")
            config_dir.mkdir(parents=True)
            config_path.write_text(
                textwrap.dedent(
                    """
                    [service]
                    state_db = "__STATE_DIR__/state.db"
                    state_dir = "__STATE_DIR__"

                    [bitbucket]
                    workspace = "example-workspace"

                    [[bitbucket.repositories]]
                    slug = "example-repo"
                    clone_url = "git@bitbucket.org:example-workspace/example-repo.git"

                    [agents]
                    strategy = "codex"

                    [agents.codex]
                    enabled = true
                    command = "/home/example/scout/Loading .bashrc
                    DEVELOPER_SETUP is set to enabled
                    setting local-env"
                    max_subagents = 20

                    [agents.claude]
                    enabled = true
                    command = "/claude"
                    """
                )
                .replace("__STATE_DIR__", str(state_dir))
                .lstrip(),
                encoding="utf-8",
            )

            write_executable(cli_dir / "codex", "#!/usr/bin/env bash\nexit 0\n")
            write_executable(cli_dir / "claude", "#!/usr/bin/env bash\nexit 0\n")
            write_executable(
                bin_dir / "id",
                """
                #!/usr/bin/env bash
                if [[ "${1:-}" == "-u" ]]; then
                  echo 0
                  exit 0
                fi
                if [[ "${1:-}" == "-gn" ]]; then
                  echo scout
                  exit 0
                fi
                if [[ "${1:-}" == "-un" ]]; then
                  echo scout-login
                  exit 0
                fi
                exec /usr/bin/id "$@"
                """,
            )
            write_executable(
                bin_dir / "getent",
                """
                #!/usr/bin/env bash
                if [[ "${1:-}" == "passwd" ]]; then
                  echo "scout-login:x:1000:1000:Scout Login:__HOME_DIR__:/bin/bash"
                  exit 0
                fi
                exec /usr/bin/getent "$@"
                """.replace("__HOME_DIR__", str(home_dir)),
            )
            write_executable(
                bin_dir / "install",
                """
                #!/usr/bin/env bash
                set -euo pipefail
                mode=""
                make_dirs=0
                args=()
                while (($#)); do
                  case "$1" in
                    -d)
                      make_dirs=1
                      shift
                      ;;
                    -m)
                      mode="$2"
                      shift 2
                      ;;
                    -o|-g)
                      shift 2
                      ;;
                    *)
                      args+=("$1")
                      shift
                      ;;
                  esac
                done
                if [[ "${make_dirs}" -eq 1 ]]; then
                  for path in "${args[@]}"; do
                    mkdir -p "${path}"
                    if [[ -n "${mode}" ]]; then chmod "${mode}" "${path}"; fi
                  done
                else
                  src="${args[0]}"
                  dest="${args[1]}"
                  mkdir -p "$(dirname "${dest}")"
                  cp "${src}" "${dest}"
                  if [[ -n "${mode}" ]]; then chmod "${mode}" "${dest}"; fi
                fi
                """,
            )
            write_executable(
                bin_dir / "runuser",
                """
                #!/usr/bin/env bash
                command_name="${@: -1}"
                echo "Loading .bashrc"
                echo "DEVELOPER_SETUP is set to enabled"
                echo "setting local-env"
                if [[ "${command_name}" == "codex" ]]; then
                  echo "__CLI_DIR__/codex"
                elif [[ "${command_name}" == "claude" ]]; then
                  echo "__CLI_DIR__/claude"
                fi
                """.replace("__CLI_DIR__", str(cli_dir)),
            )
            for command in ("chown", "systemctl"):
                write_executable(
                    bin_dir / command,
                    """
                    #!/usr/bin/env bash
                    exit 0
                    """,
                )

            env = clean_env()
            env.update(
                {
                    "PATH": "{}:/usr/bin:/bin".format(bin_dir),
                    "SCOUT_CONFIG_DIR": str(config_dir),
                    "SCOUT_SECRET_DIR": str(secret_dir),
                    "SCOUT_STATE_DIR": str(state_dir),
                    "SCOUT_LOG_DIR": str(log_dir),
                    "SCOUT_SERVICE_PATH": str(service_path),
                    "SCOUT_SCHEMA_PATH": str(schema_path),
                }
            )

            subprocess.run(
                [
                    "bash",
                    str(SETUP),
                    "--logged-in-cli-current-user",
                    "--binary",
                    "/usr/bin/scout",
                    "--config",
                    str(config_path),
                    "--bitbucket-url",
                    "https://bitbucket.org/example-workspace/example-repo/pull-requests/",
                ],
                check=True,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            config = config_path.read_text(encoding="utf-8")
            self.assertIn('command = "{}"'.format(cli_dir / "codex"), config)
            self.assertIn('command = "{}"'.format(cli_dir / "claude"), config)
            self.assertNotIn("Loading .bashrc", config)
            self.assertNotIn("DEVELOPER_SETUP", config)
            self.assertNotIn("setting local-env", config)

    def test_setup_rejects_non_bitbucket_url(self):
        if shutil.which("bash") is None:
            self.skipTest("bash is not available")

        result = subprocess.run(
            [
                "bash",
                str(SETUP),
                "--print-unit",
                "--binary",
                "/usr/bin/scout",
                "--config",
                "/etc/scout/config.toml",
                "--bitbucket-url",
                "git@github.com:org/repo.git",
            ],
            env=clean_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a Bitbucket Cloud URL", result.stderr)


if __name__ == "__main__":
    unittest.main()
