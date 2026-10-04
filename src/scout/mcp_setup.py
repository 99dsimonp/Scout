"""Apply the optional diagnostics service without changing Scout's lifecycle."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile

from .mcp_config import load_mcp_config


SERVICE = "scout-mcp.service"
READER = "scout-mcp"
RUNTIME = Path("/usr/libexec/scout-mcp/bin/python")
SNAPSHOT = Path("/etc/scout/mcp.toml")
MANIFEST = Path("/var/lib/scout-mcp-setup/firewall.json")
MCP_DROPIN = Path("/etc/systemd/system/scout-mcp.service.d/scout.conf")
SCOUT_DROPIN = Path("/etc/systemd/system/scout.service.d/diagnostics.conf")


class SetupError(RuntimeError):
    pass


def run(*args, allowed=(0,)):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in allowed:
        raise SetupError("{}: {}".format(shlex.join(args), result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def unit_value(value):
    """Quote a systemd value, including literal percent specifiers."""
    return json.dumps(str(value).replace("%", "%%"))


def firewall_rules(config, zones):
    destination = 'destination address="{}" port port="{}" protocol="tcp"'.format(
        config.bind_address, config.port
    )
    # Install the deny rule before any allows; MCP stays stopped until both sets exist.
    rules = ['rule family="ipv4" priority="-32759" {} drop'.format(destination)]
    rules.extend(
        'rule family="ipv4" priority="-32760" source address="{}" {} accept'.format(network, destination)
        for network in config.allowed_networks
    )
    return [(zone, rule) for zone in sorted(zones) for rule in rules]


def firewall_command(zone, rule, action, permanent):
    args = ["firewall-cmd"]
    if permanent:
        args.append("--permanent")
    args.extend(["--zone=" + zone, "--{}-rich-rule={}".format(action, rule)])
    return args


def read_manifest():
    if not MANIFEST.exists():
        return []
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(
        not isinstance(row, list) or len(row) != 2 or not all(isinstance(v, str) for v in row)
        for row in value
    ):
        raise SetupError("Invalid Scout MCP firewall manifest: {}".format(MANIFEST))
    return [tuple(row) for row in value]


def write_file(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".scout-mcp-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_raw(config_path):
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    with open(config_path, "rb") as stream:
        return tomllib.load(stream)


def protected_paths(raw, config):
    state = Path(config.state_dir)
    paths = [Path("/etc/scout/secrets"), Path("/run/credentials"), state / "repos", state / "worktrees",
             state / ".codex", state / ".claude", state / ".ssh", state / "agents", Path("/var/lib/scout/agents"), Path("/var/lib/scout/.ssh")]
    agents = raw.get("agents", {})
    if not isinstance(agents, dict):
        agents = {}
    for name in ("codex", "claude"):
        section = agents.get(name, {})
        home_dir = section.get("home_dir") if isinstance(section, dict) else None
        # Invalid provider settings must remain diagnosable. Only usable absolute homes
        # can hold provider credentials; the standard homes are always masked above.
        if isinstance(home_dir, str) and Path(home_dir).is_absolute() and not any(ord(c) < 32 for c in home_dir):
            paths.append(Path(home_dir))
    return sorted(set(paths))


def validate_paths(config, protected):
    data = [Path(config.state_dir), Path(config.state_dir) / "runs", Path(config.state_db), Path(config.log_path)]
    for path in data + protected:
        if not path.is_absolute() or "\n" in str(path) or "\r" in str(path):
            raise SetupError("Diagnostic and protected paths must be absolute single-line paths")
        if any(part == ".." for part in path.parts):
            raise SetupError("Paths containing '..' are not supported: {}".format(path))
        for parent in [path] + list(path.parents):
            if parent.is_symlink():
                raise SetupError("Symlink paths are not supported: {}".format(parent))
    for path in data:
        if any(path == root or root in path.parents for root in (Path("/home"), Path("/root"), Path("/run/user"))):
            raise SetupError("Diagnostic data must be outside /home, /root and /run/user (ProtectHome): {}".format(path))
    # A provider home may contain credentials even when a custom state path was chosen.
    for path in [Path(config.state_db), Path(config.state_dir) / "runs", Path(config.log_path)]:
        if any(path == root or root in path.parents for root in protected):
            raise SetupError("Diagnostic path overlaps a credential/provider/repository path: {}".format(path))
    inherited = [Path(config.state_dir) / "runs", Path(config.log_path).parent]
    if any(directory == root or directory in root.parents for directory in inherited for root in protected):
        raise SetupError("Inherited diagnostic ACLs would include a protected directory")
    if Path(config.log_path).parent in (Path("/"), Path("/var"), Path("/var/log"), Path(config.state_dir)) or Path(config.log_path).parent in Path(config.state_dir).parents:
        raise SetupError("mcp.log_path requires a dedicated diagnostics directory")


def render_snapshot(config):
    values = asdict(config)
    lines = ["# Generated by scout-setup --apply-mcp; edit the original config.toml.", "[service]",
             "state_dir = " + json.dumps(values.pop("state_dir")),
             "state_db = " + json.dumps(values.pop("state_db")), "", "[mcp]"]
    for key, value in values.items():
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, tuple):
            value = list(value)
        lines.append("{} = {}".format(key, json.dumps(value)))
    return "\n".join(lines) + "\n"


def render_mcp_dropin(config, protected):
    lines = ["[Service]", "ExecStart=", "ExecStart={} -m scout.mcp_service --config {}".format(RUNTIME, unit_value(SNAPSHOT)),
             "ReadOnlyPaths={} {} {}".format(unit_value(config.state_dir), unit_value(Path(config.state_db).parent), unit_value(Path(config.log_path).parent)),
             "InaccessiblePaths=" + " ".join(unit_value("-" + str(path)) for path in protected),
             "IPAddressDeny=any"]
    lines.extend("IPAddressAllow=" + network for network in config.allowed_networks)
    return "\n".join(lines) + "\n"


def render_scout_dropin(config, retention_days=7):
    return "\n".join([
        "[Service]", 'Environment="SCOUT_DIAGNOSTIC_READER=scout-mcp"',
        "Environment=" + unit_value("SCOUT_DIAGNOSTIC_LOG=" + str(config.log_path)),
        'Environment="SCOUT_DIAGNOSTIC_RETENTION_DAYS={}"'.format(retention_days),
        "ReadWritePaths=" + unit_value(Path(config.log_path).parent), "",
    ])


def grant_access(config):
    from .diagnostic_access import set_diagnostic_acl

    user = run("systemctl", "show", "scout.service", "--property=User", "--value") or "scout"
    owner = pwd.getpwnam(user)
    try:
        pwd.getpwnam(READER)
    except KeyError:
        run("useradd", "--system", "--user-group", "--home-dir", "/nonexistent", "--shell", "/sbin/nologin", READER)
    state = Path(config.state_dir)
    log_dir = Path(config.log_path).parent
    for directory in (state, state / "runs", log_dir):
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o750)
            os.chown(directory, owner.pw_uid, owner.pw_gid)
    for path in (state, Path(config.state_db).parent, log_dir):
        for parent in [path] + list(path.parents)[:-1]:
            set_diagnostic_acl(parent, READER, "--x")
    # No inherited ACL on the state root: provider homes and credentials also live there.
    for directory in (state / "runs", log_dir):
        for root, dirs, files in os.walk(directory, followlinks=False):
            root_path = Path(root)
            if root_path.is_symlink():
                continue
            set_diagnostic_acl(root_path, READER, "r-x", inherit=True)
            dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
            for name in files:
                path = root_path / name
                if path.is_file() and not path.is_symlink():
                    set_diagnostic_acl(path, READER, "r--")
    for path in [Path(config.state_db), Path(str(config.state_db) + "-wal"), Path(str(config.state_db) + "-shm"),
                 state / "review-log.jsonl", state / "provider-usage.jsonl"]:
        if path.is_file() and not path.is_symlink():
            set_diagnostic_acl(path, READER, "r--")


def stop_mcp():
    loaded = run("systemctl", "show", SERVICE, "--property=LoadState", "--value", allowed=(0, 1)) != "not-found"
    if loaded:
        run("systemctl", "disable", "--now", SERVICE)


def apply(config_path):
    config = load_mcp_config(config_path)
    if os.geteuid() != 0:
        raise SetupError("Run scout-setup --apply-mcp as root")
    if not config.enabled:
        # Closing the listener must succeed even when stale firewall metadata cannot be read.
        stop_mcp()
    old_rules = read_manifest()
    new_rules = []
    protected = []
    if config.enabled:
        raw = read_raw(config_path)
        retention_days = raw.get("service", {}).get("retention_days", 7)
        if type(retention_days) is not int or retention_days < 1:
            raise SetupError("service.retention_days must be a positive integer")
        retention_days = min(retention_days, 7)
        protected = protected_paths(raw, config)
        validate_paths(config, protected)
        if not RUNTIME.is_file():
            raise SetupError("Install the matching scout-mcp-runtime RPM before enabling MCP")
        for command in ("systemctl", "firewall-cmd", "setfacl", "getfacl", "useradd"):
            if shutil.which(command) is None:
                raise SetupError("Required command is missing: {}".format(command))
        run("firewall-cmd", "--state")
        zones = run("firewall-cmd", "--get-zones").split()
        if not zones:
            raise SetupError("firewalld returned no zones")
        new_rules = firewall_rules(config, zones)
        for zone, rule in new_rules:
            if (zone, rule) in old_rules:
                continue
            for permanent in (False, True):
                if run(*firewall_command(zone, rule, "query", permanent), allowed=(0, 1)) == "yes":
                    raise SetupError("An unowned firewall rule conflicts with Scout MCP; no changes applied")
    paths = (SNAPSHOT, MCP_DROPIN, SCOUT_DROPIN, MANIFEST)
    previous = {path: (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_uid, path.stat().st_gid) if path.exists() else None for path in paths}
    added, removed = [], []
    if config.enabled:
        stop_mcp()
    try:
        if not config.enabled and old_rules:
            run("firewall-cmd", "--state")
        if config.enabled:
            grant_access(config)
            write_file(SNAPSHOT, render_snapshot(config), 0o640)
            os.chown(SNAPSHOT, 0, pwd.getpwnam(READER).pw_gid)
            write_file(MCP_DROPIN, render_mcp_dropin(config, protected), 0o644)
            write_file(SCOUT_DROPIN, render_scout_dropin(config, retention_days), 0o644)
        for zone, rule in old_rules:
            for permanent in (False, True):
                run(*firewall_command(zone, rule, "remove", permanent))
                removed.append((zone, rule, permanent))
        for zone, rule in new_rules:
            for permanent in (False, True):
                run(*firewall_command(zone, rule, "add", permanent))
                added.append((zone, rule, permanent))
        if config.enabled:
            write_file(MANIFEST, json.dumps(new_rules))
        else:
            for path in paths:
                path.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        if config.enabled:
            run("systemctl", "enable", "--now", SERVICE)
    except (OSError, SetupError, subprocess.SubprocessError) as error:
        failures = []
        for args in [("systemctl", "stop", SERVICE)] + [tuple(firewall_command(z, r, "remove", p)) for z, r, p in reversed(added)] + [tuple(firewall_command(z, r, "add", p)) for z, r, p in removed]:
            try:
                run(*args)
            except (OSError, SetupError) as rollback_error:
                failures.append(str(rollback_error))
        for path, saved in previous.items():
            try:
                if saved is None:
                    path.unlink(missing_ok=True)
                else:
                    write_file(path, saved[0], saved[1])
                    os.chown(path, saved[2], saved[3])
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        try:
            run("systemctl", "daemon-reload")
        except (OSError, SetupError) as rollback_error:
            failures.append(str(rollback_error))
        raise SetupError("{}; MCP left stopped.{}".format(error, " Rollback errors: " + "; ".join(failures) if failures else "")) from error
    if config.enabled:
        url = "http://{}:{}/mcp".format(config.hostname, config.port)
        print("Scout MCP: " + url)
        print("codex mcp add scout --url " + shlex.quote(url))
        print("claude mcp add --scope user --transport http scout " + shlex.quote(url))
        print("To apply diagnostic logging and file-access hooks, restart Scout during a suitable window:")
        print("  sudo systemctl restart scout")
    else:
        print("Scout MCP disabled. Scout was not restarted or stopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/scout/config.toml")
    args = parser.parse_args(argv)
    try:
        apply(args.config)
    except (ValueError, OSError, SetupError, KeyError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
