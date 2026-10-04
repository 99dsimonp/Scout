import tempfile
import unittest
from pathlib import Path

from scout.config import ConfigError
from scout.mcp_config import McpConfig, load_mcp_config, parse_mcp_config


class McpConfigTests(unittest.TestCase):
    def enabled(self, **settings):
        section = {
            "enabled": True,
            "bind_address": "10.20.30.40",
            "hostname": "scout.internal.example",
            "allowed_networks": ["10.80.0.0/16", "100.64.0.0/10"],
        }
        section.update(settings)
        return {"mcp": section}

    def test_disabled_ignores_unrelated_and_enabled_only_settings(self):
        self.assertEqual(parse_mcp_config({}), McpConfig())
        self.assertEqual(parse_mcp_config({
            "mcp": {"enabled": False, "port": "invalid", "bind_address": "0.0.0.0"},
            "service": [], "agents": "broken", "bitbucket": "broken",
        }), McpConfig())

    def test_enabled_defaults_and_diagnostic_paths(self):
        raw = self.enabled()
        raw["service"] = {"state_dir": "/srv/scout", "state_db": "/srv/db/scout.db"}
        raw["agents"] = {"codex": {"enabled": "invalid"}}
        config = parse_mcp_config(raw)
        self.assertEqual(config.state_dir, "/srv/scout")
        self.assertEqual(config.state_db, "/srv/db/scout.db")
        self.assertEqual(config.log_path, "/var/log/scout/diagnostics/scout.log")
        self.assertEqual(config.allowed_networks, ("10.80.0.0/16", "100.64.0.0/10"))
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.max_records, 200)
        self.assertEqual(config.max_bytes, 65536)
        self.assertEqual(config.max_scan_bytes, 8388608)
        self.assertEqual(config.query_timeout_seconds, 5)
        del raw["service"]["state_db"]
        self.assertEqual(parse_mcp_config(raw).state_db, "/srv/scout/state.db")

    def test_load_reads_only_diagnostic_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('agents = "broken"\n[mcp]\nenabled = false\n', encoding="utf-8")
            self.assertFalse(load_mcp_config(str(path)).enabled)

    def test_bind_must_be_private_ipv4_unicast(self):
        for address in ("0.0.0.0", "127.0.0.1", "169.254.1.2", "8.8.8.8", "224.1.2.3",
                        "255.255.255.255", "::1", "hostname", "100.64.1.1", 1):
            with self.subTest(address=address), self.assertRaises(ConfigError):
                parse_mcp_config(self.enabled(bind_address=address))
        for address in ("10.1.2.3", "172.16.1.2", "192.168.3.4"):
            self.assertEqual(parse_mcp_config(self.enabled(bind_address=address)).bind_address, address)

    def test_enabled_requires_explicit_network_boundary(self):
        for key in ("bind_address", "hostname", "allowed_networks"):
            raw = self.enabled()
            del raw["mcp"][key]
            with self.subTest(key=key), self.assertRaises(ConfigError):
                parse_mcp_config(raw)
        for networks in ([], "10.0.0.0/8", ["0.0.0.0/0"], ["::/0"], ["10.0.1.2/8"], [1]):
            with self.subTest(networks=networks), self.assertRaises(ConfigError):
                parse_mcp_config(self.enabled(allowed_networks=networks))

    def test_hostname_cannot_contain_url_port_wildcard_or_headers(self):
        for hostname in ("", "http://scout", "scout:8765", "*.example", "scout/", "scout\r\nHost: x",
                         "-scout", "scout..example", "scout.", "a" * 64, 1):
            with self.subTest(hostname=hostname), self.assertRaises(ConfigError):
                parse_mcp_config(self.enabled(hostname=hostname))
        self.assertEqual(parse_mcp_config(self.enabled(hostname="SCOUT.internal")).hostname, "scout.internal")

    def test_limits_reject_wrong_types_and_out_of_bounds_values(self):
        for key, bad_values in {
            "port": (0, 65536, True, "8765"),
            "max_records": (0, 201, True, 1.5),
            "max_bytes": (0, 2047, 65537, False),
            "max_scan_bytes": (0, 67108865, True),
            "query_timeout_seconds": (0, 31, True, float("inf")),
        }.items():
            for value in bad_values:
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    parse_mcp_config(self.enabled(**{key: value}))
        self.assertEqual(parse_mcp_config(self.enabled(query_timeout_seconds=0.5)).query_timeout_seconds, 0.5)
        self.assertEqual(parse_mcp_config(self.enabled(max_bytes=2048)).max_bytes, 2048)

    def test_diagnostic_paths_must_be_absolute_and_normalized(self):
        for path in ("relative", "/tmp/../secret", "/tmp/with\nnewline", "/tmp/with\x00null"):
            for key in ("state_dir", "state_db", "log_path"):
                raw = self.enabled()
                raw["service"] = {}
                raw["mcp" if key == "log_path" else "service"][key] = path
                with self.subTest(key=key, path=path), self.assertRaises(ConfigError):
                    parse_mcp_config(raw)

    def test_sections_and_enabled_have_strict_types(self):
        for raw in ({"mcp": []}, {"mcp": {"enabled": "false"}}, {"mcp": {"enabled": 1}}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_mcp_config(raw)
        raw = self.enabled()
        raw["service"] = []
        with self.assertRaises(ConfigError):
            parse_mcp_config(raw)


if __name__ == "__main__":
    unittest.main()
