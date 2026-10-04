from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

from .config import ConfigError, tomllib


@dataclass(frozen=True)
class McpConfig:
    enabled: bool = False
    bind_address: str = ""
    port: int = 8765
    hostname: str = ""
    allowed_networks: Tuple[str, ...] = ()
    state_dir: str = "/var/lib/scout"
    state_db: str = "/var/lib/scout/state.db"
    log_path: str = "/var/log/scout/diagnostics/scout.log"
    max_records: int = 200
    max_bytes: int = 65536
    max_scan_bytes: int = 8388608
    query_timeout_seconds: float = 5


def load_mcp_config(path: str) -> McpConfig:
    with open(path, "rb") as stream:
        return parse_mcp_config(tomllib.load(stream))


def parse_mcp_config(raw: Dict[str, Any]) -> McpConfig:
    section = raw.get("mcp", {})
    if not isinstance(section, dict):
        raise ConfigError("mcp must be a table")
    enabled = section.get("enabled", False)
    if type(enabled) is not bool:
        raise ConfigError("mcp.enabled must be a boolean")
    if not enabled:
        return McpConfig()

    service = raw.get("service", {})
    if not isinstance(service, dict):
        raise ConfigError("service must be a table for MCP diagnostic paths")
    bind_address = section.get("bind_address")
    try:
        address = ipaddress.IPv4Address(bind_address) if isinstance(bind_address, str) else None
    except ipaddress.AddressValueError:
        address = None
    private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if address is None or not any(address in ipaddress.IPv4Network(net) for net in private_networks):
        raise ConfigError("mcp.bind_address must be an explicit private RFC1918 IPv4 address")

    hostname = section.get("hostname")
    if (not isinstance(hostname, str) or len(hostname) > 253
            or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in hostname.split("."))):
        raise ConfigError("mcp.hostname must be a DNS hostname without a scheme, port, or wildcard")
    networks = section.get("allowed_networks")
    if not isinstance(networks, list) or not networks:
        raise ConfigError("mcp.allowed_networks must contain explicit company/VPN IPv4 CIDRs")
    normalized_networks = []
    for network in networks:
        try:
            parsed = ipaddress.IPv4Network(network) if isinstance(network, str) else None
        except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
            parsed = None
        if parsed is None or parsed.prefixlen == 0:
            raise ConfigError("mcp.allowed_networks must contain IPv4 CIDRs with network addresses; /0 is forbidden")
        normalized_networks.append(str(parsed))

    state_dir = _absolute_path(service.get("state_dir", "/var/lib/scout"), "service.state_dir")
    state_db = _absolute_path(service.get("state_db", str(Path(state_dir) / "state.db")), "service.state_db")
    timeout = section.get("query_timeout_seconds", 5)
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30):
        raise ConfigError("mcp.query_timeout_seconds must be greater than zero and at most 30")
    return McpConfig(
        enabled=True,
        bind_address=str(address),
        port=_limit(section, "port", 8765, 65535),
        hostname=hostname.lower(),
        allowed_networks=tuple(normalized_networks),
        state_dir=state_dir,
        state_db=state_db,
        log_path=_absolute_path(section.get("log_path", "/var/log/scout/diagnostics/scout.log"), "mcp.log_path"),
        max_records=_limit(section, "max_records", 200, 200),
        max_bytes=_limit(section, "max_bytes", 65536, 65536, minimum=2048),
        max_scan_bytes=_limit(section, "max_scan_bytes", 8388608, 67108864),
        query_timeout_seconds=timeout,
    )


def _limit(section: Dict[str, Any], name: str, default: int, maximum: int, minimum: int = 1) -> int:
    value = section.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError("mcp.{} must be an integer between {} and {}".format(name, minimum, maximum))
    return value


def _absolute_path(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or ".." in Path(value).parts or any(ord(character) < 32 for character in value)):
        raise ConfigError("{} must be an absolute path without parent traversal or control characters".format(name))
    return value
