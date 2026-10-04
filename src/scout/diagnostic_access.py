"""Best-effort access to the small set of files shared with diagnostics."""
from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from pathlib import Path

LOG = logging.getLogger(__name__)


def diagnostic_reader() -> str:
    reader = os.environ.get("SCOUT_DIAGNOSTIC_READER", "")
    if reader and not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?|[0-9]+", reader):
        _warn("invalid diagnostic reader account")
        return ""
    return reader


def grant_diagnostic_file(path: Path) -> None:
    _grant(path, directory=False)


def grant_diagnostic_traversal(path: Path) -> None:
    # The state root also holds credentials and checkouts. Only the explicitly
    # shared files and directories receive read or inherited ACL entries.
    _grant(path, directory=True)


def prepare_diagnostic_directory(path: Path) -> None:
    if not diagnostic_reader():
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
        _grant(path, directory=True, inherit=True)
    except OSError as exc:
        _warn("cannot prepare diagnostic directory path=%s error=%s", path, exc)


def _grant(path: Path, directory: bool, inherit: bool = False) -> None:
    reader = diagnostic_reader()
    if not reader:
        return
    try:
        permissions = "r-x" if inherit else "--x" if directory else "r--"
        set_diagnostic_acl(path, reader, permissions, inherit=inherit)
    except FileNotFoundError as exc:
        # A missing optional artifact is normal; missing ACL utilities are not.
        if path.exists():
            _warn("cannot grant diagnostic access path=%s error=%s", path, exc)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn("cannot grant diagnostic access path=%s error=%s", path, exc)


def open_diagnostic_path(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open relative to held directory descriptors, refusing every symlink."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise OSError("diagnostic paths must not contain parent traversal")
    parent = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            next_parent = os.open(
                part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            os.close(parent)
            parent = next_parent
        return os.open(
            path.name or ".", flags | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode, dir_fd=parent,
        )
    finally:
        os.close(parent)


def set_diagnostic_acl(path: Path, reader: str, permissions: str, *, inherit: bool = False) -> None:
    """Grant reader permissions without unmasking any other ACL entry.

    Setup uses this strict helper; producer hooks catch its errors so diagnostics
    cannot prevent a review from running.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?|[0-9]+", reader):
        raise ValueError("invalid diagnostic reader account")
    if not re.fullmatch(r"[r-][w-][x-]", permissions):
        raise ValueError("invalid diagnostic reader permissions")
    fd = open_diagnostic_path(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)) or (inherit and not stat.S_ISDIR(mode)):
            raise OSError("diagnostic path has an unexpected file type")
        descriptor = "/proc/self/fd/{}".format(fd)
        acl = subprocess.run(
            ["getfacl", "--omit-header", "--numeric", "--all-effective", "--", descriptor],
            pass_fds=(fd,), check=True, capture_output=True, text=True, timeout=1,
            env=dict(os.environ, LC_ALL="C"),
        ).stdout
        existing = {}
        changes = []
        for line in acl.splitlines():
            if not line or line.startswith("#"):
                continue
            entry, _, effective = line.partition("#effective:")
            key, rights = entry.strip().rsplit(":", 1)
            existing[key] = effective.strip() or rights
            # getfacl computes effective permissions using each ACL's mask.
            # Freeze those entries before widening that mask for the reader.
            if effective and (inherit or not key.startswith("default:")):
                changes.append(key + ":" + effective.strip())
        if "group:" not in existing:
            raise OSError("getfacl did not return a valid access ACL")
        for prefix in ("", "default:") if inherit else ("",):
            old_mask = existing.get(prefix + "mask:", existing.get(prefix + "group:", existing["group:"]))
            mask = "".join(bit if bit in old_mask + permissions else "-" for bit in "rwx")
            changes.extend([
                "{}user:{}:{}".format(prefix, reader, permissions),
                "{}mask::{}".format(prefix, mask),
            ])
            if prefix and prefix + "group:" not in existing:
                changes.append(prefix + "group::" + existing["group:"])
        subprocess.run(
            ["setfacl", "--no-mask", "-m", ",".join(changes), "--", descriptor],
            pass_fds=(fd,), check=True, capture_output=True, timeout=1,
        )
    finally:
        os.close(fd)


def _warn(message: str, *args) -> None:
    # Do not send handler/ACL failures back through the diagnostic file handler.
    try:
        LOG.warning(message, *args, extra={"diagnostic_internal": True})
    except Exception:
        pass
