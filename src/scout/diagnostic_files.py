"""Bounded file reads for diagnostics; never follow a provider-supplied path."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path


# A failed prefixed-key match must not restart at every hyphen in one name;
# consume leading CLI option dashes only at that same name boundary.
_SECRET = re.compile(
    r"(?i)((?<![a-z0-9_-])(?:--?)?(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"""client[_-]?secret|password|passwd|secret|token)\b["\']?\s*[:=]\s*)"""
    r"""(?:"[^"\r\n]*"?|\'[^\'\r\n]*\'?|[^\s"\'\\,;}\]]+)"""
)
_AUTH = re.compile(r"""(?i)(\bauthorization["\']?\s*[:=]\s*["\']?(?:bearer|basic)\s+)[^\s"\'\\,;}]+""")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}|ATATT[A-Za-z0-9_=-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_KEY_BOUNDARY = re.compile(r"-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----")
_KEY_MARKERS = tuple("-----" + action + " " + kind + "PRIVATE KEY-----"
                     for action in ("BEGIN", "END")
                     for kind in ("", "RSA ", "DSA ", "EC ", "OPENSSH ", "ENCRYPTED "))
_MARKER_PREFIXES = sorted({marker[:length] for marker in _KEY_MARKERS
                          for length in range(1, len(marker))}, key=len, reverse=True)


def _key_state(text, active, marker_prefix):
    """Track PEM boundaries even in omitted records and across scan limits.

    Keep only an unfinished, public boundary marker in the cursor. Keeping an
    arbitrary text suffix here would expose secret key bytes in that cursor.
    """
    text = marker_prefix + text
    sensitive = active
    for boundary in _KEY_BOUNDARY.finditer(text):
        active = boundary[1] == "BEGIN"
        sensitive = sensitive or active
    prefix = next((value for value in _MARKER_PREFIXES if text.endswith(value)), "")
    return active, prefix, sensitive


def redact(value):
    if isinstance(value, str):
        value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
        value = _AUTH.sub(r"\1[REDACTED]", value)
        value = _SECRET.sub(r"\1[REDACTED]", value)
        value = _TOKEN.sub("[REDACTED]", value)
        return re.sub(r"(https?://)[^\s/@]+@", r"\1[REDACTED]@", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def json_size(value):
    # Default JSON escaping is larger than the transport's compact UTF-8 form.
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


class InvalidCursor(ValueError):
    pass


class CursorCodec:
    """Cursors are local to this service process and cannot supply arbitrary offsets."""
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def encode(self, scope, position):
        payload = json.dumps([scope, position], separators=(",", ":")).encode()
        signature = hmac.new(self.key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + payload).decode()

    def decode(self, token, scope):
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError()
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.new(self.key, payload, hashlib.sha256).digest()):
                raise ValueError()
            saved_scope, position = json.loads(payload)
            if saved_scope != scope or not isinstance(position, dict):
                raise ValueError()
            return position
        except (ValueError, TypeError, UnicodeError):
            raise InvalidCursor("Cursor is invalid or the service restarted; restart without a cursor.") from None


@contextmanager
def open_regular(path):
    """Open each component relative to an already-open directory, refusing symlinks.

    Holding the parent descriptor prevents a rename followed by a symlink swap
    from redirecting the final open into credentials or a provider home.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("Unsafe diagnostic path")
    parent = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = None
    try:
        for part in path.parts[1:-1]:
            next_parent = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = next_parent
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Diagnostic source is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            yield handle
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def read_page(path, codec, scope, cursor, *, record_limit, byte_limit, scan_limit,
              timeout, transform=None, allow_unterminated=False):
    """Read complete bounded records; oversized records are omitted, never split.

    A cursor carries discard/private-key state so a later page cannot expose the
    remainder of a credential or oversized JSON record as ordinary text.
    """
    saved = codec.decode(cursor, scope) if cursor is not None else {}
    deadline = time.monotonic() + timeout
    with open_regular(path) as handle:
        info = os.fstat(handle.fileno())
        identity = [info.st_dev, info.st_ino]
        offset = saved.get("offset", 0)
        if saved and (saved.get("file") != identity or offset > info.st_size or (
                allow_unterminated and saved.get("mtime") != info.st_mtime_ns)):
            return {"status": "stale_cursor", "reason": "Source rotated, was pruned, or was replaced; restart without a cursor."}
        handle.seek(offset)
        records, scanned, omitted = [], 0, 0
        pending = False
        discard = saved.get("discard", False)
        private_key = saved.get("private_key", False)
        marker_prefix = saved.get("marker_prefix", "")
        examined = 0
        # Reserve room for cursors, counters, and the MCP adapter's metadata.
        record_bytes = max(128, byte_limit - 1536)
        while scanned < scan_limit and examined < record_limit and time.monotonic() < deadline:
            start = handle.tell()
            previous_key_state = private_key, marker_prefix
            chunk = handle.readline(min(record_bytes + 1, scan_limit - scanned, info.st_size - start))
            scanned += len(chunk)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            private_key, marker_prefix, sensitive = _key_state(text, private_key, marker_prefix)
            complete = chunk.endswith(b"\n")
            if discard:
                discard = not complete
                if complete:
                    examined += 1
                continue
            if not complete and handle.tell() < info.st_size:
                discard = True
                omitted += 1
                continue
            if not complete and not allow_unterminated:
                # Retry the final line after Scout has appended its newline.
                handle.seek(start)
                private_key, marker_prefix = previous_key_state
                pending = True
                break
            examined += 1
            if len(chunk) > record_bytes:
                omitted += 1
                continue
            text = "[REDACTED PRIVATE KEY]" if sensitive else text.rstrip("\r\n")
            item = transform(text) if transform is not None else redact(text)
            if item is None:
                omitted += 1
                continue
            if json_size(records + [item]) > record_bytes:
                if not records:
                    omitted += 1
                    continue
                handle.seek(start)
                private_key, marker_prefix = previous_key_state
                break
            records.append(item)
        offset = handle.tell()
        position = dict(file=identity, offset=offset, discard=discard, private_key=private_key,
                        marker_prefix=marker_prefix)
        if allow_unterminated:
            position["mtime"] = info.st_mtime_ns
        return {
            "status": "ok", "records": records,
            "next_cursor": codec.encode(scope, position),
            "truncated": offset < info.st_size or omitted > 0 or discard,
            "partial_line": pending, "omitted_records": omitted,
            "scanned_bytes": scanned, "has_more": offset < info.st_size,
        }
