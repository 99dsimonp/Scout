import contextlib
from dataclasses import replace
import io
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scout import mcp_setup
from scout.mcp_config import McpConfig, load_mcp_config


class McpSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = McpConfig(enabled=True, bind_address="10.20.0.5", hostname="scout.company.test",
                                allowed_networks=("10.30.0.0/16",), state_dir=str(self.root / "state"),
                                state_db=str(self.root / "state/state.db"), log_path=str(self.root / "logs/scout.log"))
        self.calls = []
        for name in ("SNAPSHOT", "MANIFEST", "MCP_DROPIN", "SCOUT_DROPIN"):
            patcher = patch.object(mcp_setup, name, self.root / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.start_patch("load_mcp_config", return_value=self.config)
        self.start_patch("read_raw", return_value={})
        self.start_patch("grant_access")
        self.start_patch("run", side_effect=self.command)
        for target in ("scout.mcp_setup.os.geteuid", "scout.mcp_setup.os.chown", "scout.mcp_setup.shutil.which", "scout.mcp_setup.pwd.getpwnam"):
            patcher = patch(target)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith("geteuid"):
                mocked.return_value = 0
        self.runtime = self.root / "python"
        self.runtime.touch()
        self.start_patch("RUNTIME", self.runtime)

    def start_patch(self, name, *args, **kwargs):
        patcher = patch.object(mcp_setup, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if "--get-zones" in args:
            return "public trusted"
        if any(arg.startswith("--query-rich-rule=") for arg in args):
            return "no"
        return "loaded"

    def apply(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            mcp_setup.apply("unused.toml")
        return output.getvalue()

    def test_enabled_configuration_roundtrips_without_provider_secrets(self):
        output = self.apply()
        self.assertIn("codex mcp add scout --url http://scout.company.test:8765/mcp", output)
        self.assertIn("claude mcp add --scope user --transport http scout http://scout.company.test:8765/mcp", output)
        self.assertEqual(load_mcp_config(str(mcp_setup.SNAPSHOT)), self.config)
        snapshot = mcp_setup.SNAPSHOT.read_text()
        self.assertNotIn("bitbucket", snapshot)
        self.assertNotIn("credentials", snapshot)
        self.assertIn(("systemctl", "enable", "--now", "scout-mcp.service"), self.calls)
        self.assertFalse(any("scout.service" in call for call in self.calls))
        self.assertFalse(any("--reload" in call for call in self.calls))
        added = [call for call in self.calls if any(arg.startswith("--add-rich-rule") for arg in call)]
        self.assertEqual(len(added), 8)
        self.assertTrue(added[0][-1].endswith("drop"))
        self.assertTrue(added[2][-1].endswith("accept"))

    def test_disabled_requires_no_runtime_and_removes_only_recorded_rules(self):
        self.start_patch("load_mcp_config", return_value=McpConfig())
        self.runtime.unlink()
        owned = [["public", 'rule family="ipv4" priority="-32759" port port="8765" protocol="tcp" drop']]
        mcp_setup.MANIFEST.write_text(json.dumps(owned))
        self.apply()
        removed = [call for call in self.calls if any(arg.startswith("--remove-rich-rule") for arg in call)]
        self.assertEqual(len(removed), 2)
        self.assertTrue(all(call[-1] == "--remove-rich-rule=" + owned[0][1] for call in removed))
        self.assertNotIn(("systemctl", "enable", "--now", "scout-mcp.service"), self.calls)
        self.assertFalse(mcp_setup.MANIFEST.exists())

    def test_disabled_stops_listener_even_when_firewall_cleanup_fails(self):
        self.start_patch("load_mcp_config", return_value=McpConfig())
        mcp_setup.MANIFEST.write_text(json.dumps([["public", "owned-rule"]]))
        def down(*args, **kwargs):
            if args == ("firewall-cmd", "--state"):
                raise mcp_setup.SetupError("firewalld is down")
            return self.command(*args, **kwargs)
        self.start_patch("run", side_effect=down)
        with self.assertRaisesRegex(mcp_setup.SetupError, "firewalld is down"):
            self.apply()
        self.assertIn(("systemctl", "disable", "--now", "scout-mcp.service"), self.calls)

    def test_disabled_stops_listener_before_reading_damaged_manifest(self):
        self.start_patch("load_mcp_config", return_value=McpConfig())
        mcp_setup.MANIFEST.write_text("not json")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertIn(("systemctl", "disable", "--now", "scout-mcp.service"), self.calls)

    def test_respects_shorter_configured_diagnostic_retention(self):
        self.start_patch("read_raw", return_value={"service": {"retention_days": 1}})
        self.apply()
        self.assertIn("SCOUT_DIAGNOSTIC_RETENTION_DAYS=1", mcp_setup.SCOUT_DROPIN.read_text())

    def test_invalid_configuration_has_no_system_side_effects(self):
        self.start_patch("load_mcp_config", side_effect=ValueError("invalid bind address"))
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.calls, [])

    def test_install_failure_rolls_back_files_and_rules_and_leaves_mcp_stopped(self):
        mcp_setup.MCP_DROPIN.write_text("old config")
        old = [("public", "old-owned-rule")]
        mcp_setup.MANIFEST.write_text(json.dumps(old))
        def fail_start(*args, **kwargs):
            self.command(*args, **kwargs)
            if args == ("systemctl", "enable", "--now", "scout-mcp.service"):
                raise mcp_setup.SetupError("start failed")
            return self.command(*args, **kwargs)
        self.start_patch("run", side_effect=fail_start)
        with self.assertRaisesRegex(mcp_setup.SetupError, "MCP left stopped"):
            self.apply()
        self.assertEqual(mcp_setup.MCP_DROPIN.read_text(), "old config")
        self.assertEqual(json.loads(mcp_setup.MANIFEST.read_text()), [list(row) for row in old])
        self.assertIn(("systemctl", "stop", "scout-mcp.service"), self.calls)
        self.assertTrue(any(call[-1] == "--add-rich-rule=old-owned-rule" for call in self.calls))
        self.assertFalse(mcp_setup.SNAPSHOT.exists())

    def test_conflicting_unowned_rule_is_not_deleted(self):
        self.start_patch("run", side_effect=lambda *args, **kwargs: "public" if "--get-zones" in args else "yes")
        with self.assertRaisesRegex(mcp_setup.SetupError, "unowned firewall rule"):
            self.apply()
        self.assertFalse(mcp_setup.MANIFEST.exists())

    def test_sandbox_masks_custom_provider_homes_and_escapes_specifiers(self):
        paths = mcp_setup.protected_paths({"agents": {"codex": {"home_dir": "/opt/private codex%data"}}}, self.config)
        unit = mcp_setup.render_mcp_dropin(self.config, paths)
        self.assertIn('"-/opt/private codex%%data"', unit)
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("IPAddressAllow=10.30.0.0/16", unit)
        self.assertNotIn("ReadWritePaths", unit)

    def test_refuses_symlink_paths_and_hidden_home_data(self):
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real", target_is_directory=True)
        with self.assertRaisesRegex(mcp_setup.SetupError, "Symlink"):
            mcp_setup.validate_paths(replace(self.config, log_path=str(self.root / "link/scout.log")), [])
        with self.assertRaisesRegex(mcp_setup.SetupError, "ProtectHome"):
            mcp_setup.validate_paths(replace(self.config, state_dir="/home/scout/state"), [])

    def test_refuses_existing_log_file_symlink(self):
        log = Path(self.config.log_path)
        log.parent.mkdir()
        sensitive = self.root / "credential"
        sensitive.write_text("secret")
        log.symlink_to(sensitive)
        with self.assertRaisesRegex(mcp_setup.SetupError, "Symlink"):
            mcp_setup.validate_paths(self.config, [])
        self.assertEqual(sensitive.read_text(), "secret")

    def test_refuses_log_acl_inheritance_on_shared_state_directory(self):
        with self.assertRaisesRegex(mcp_setup.SetupError, "dedicated diagnostics directory"):
            mcp_setup.validate_paths(replace(self.config, log_path=str(self.root / "state/scout.log")), [])


class McpSetupPermissionTests(unittest.TestCase):
    def test_reader_can_read_diagnostics_but_not_provider_credentials(self):
        if os.geteuid() != 0 or not shutil.which("setfacl") or not shutil.which("runuser"):
            self.skipTest("requires the isolated Rocky RPM test container")
        try:
            pwd.getpwnam("scout-mcp")
        except KeyError:
            self.skipTest("requires the RPM-created scout-mcp account")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            (state / "runs/1").mkdir(parents=True)
            (state / "agents").mkdir()
            database = state / "state.db"
            output = state / "runs/1/codex-output.txt"
            secret = state / "agents/secret"
            for path in (database, output, secret):
                path.write_text("test")
                path.chmod(0o600)
            config = McpConfig(state_dir=str(state), state_db=str(database), log_path=str(root / "logs/scout.log"))
            subprocess.run(["setfacl", "-m", "u:61124:rwx,m::--x", str(state)], check=True)
            real_run = mcp_setup.run
            def run_in_temporary_tree(*args, **kwargs):
                if args[0] == "systemctl":
                    return pwd.getpwuid(os.geteuid()).pw_name
                if args[0] == "setfacl" and not args[-1].startswith(str(root)):
                    return ""
                return real_run(*args, **kwargs)
            with patch.object(mcp_setup, "run", side_effect=run_in_temporary_tree):
                mcp_setup.grant_access(config)
            def readable(path):
                return subprocess.run(["runuser", "-u", "scout-mcp", "--", "test", "-r", str(path)]).returncode == 0
            self.assertTrue(readable(database))
            self.assertTrue(readable(output))
            self.assertFalse(readable(secret))
            later = state / "runs/1/later-output.txt"
            later.write_text("later")
            self.assertTrue(readable(later))
            state_acl = subprocess.check_output(["getfacl", "--omit-header", str(state)], text=True)
            self.assertNotIn("default:", state_acl)
            effective_acl = subprocess.check_output(["getfacl", "--all-effective", "--numeric", str(state)], text=True)
            unrelated = next(line for line in effective_acl.splitlines() if line.startswith("user:61124:"))
            self.assertIn("#effective:--x", unrelated)



if __name__ == "__main__":
    unittest.main()
