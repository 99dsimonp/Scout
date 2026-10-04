import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scout.mcp_config import McpConfig
from scout.mcp_service import PrivateNetworkGuard, create_app, main


class PrivateNetworkGuardTests(unittest.TestCase):
    def request(self, client, forwarded=None, scope_type="http"):
        messages = []

        async def application(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
        scope = {"type": scope_type, "client": client, "headers": headers}
        app = PrivateNetworkGuard(application, ("10.80.0.0/16", "100.64.0.0/10"))
        asyncio.run(app(scope, receive, send))
        return messages

    def test_allows_actual_company_and_vpn_peers(self):
        for peer in ("10.80.3.4", "100.90.1.2"):
            self.assertEqual(self.request((peer, 1234))[0]["status"], 204)

    def test_denies_unknown_outside_and_ipv6_peers(self):
        for client in (None, ("8.8.8.8", 1234), ("10.81.0.1", 1234), ("::1", 1234), ("bad", 1234)):
            with self.subTest(client=client):
                self.assertEqual(self.request(client)[0]["status"], 403)

    def test_forwarded_headers_cannot_grant_or_revoke_access(self):
        self.assertEqual(self.request(("8.8.8.8", 1234), "10.80.3.4")[0]["status"], 403)
        self.assertEqual(self.request(("10.80.3.4", 1234), "8.8.8.8")[0]["status"], 204)

    def test_websocket_is_not_a_diagnostic_transport(self):
        self.assertEqual(self.request(("10.80.3.4", 1234), scope_type="websocket"),
                         [{"type": "websocket.close", "code": 1008}])


class McpStartupTests(unittest.TestCase):
    def run_isolated(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(config, encoding="utf-8")
            script = '''
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('mcp', 'uvicorn') or fullname == 'scout.daemon':
            raise ModuleNotFoundError('blocked optional runtime: ' + fullname)
sys.meta_path.insert(0, BlockOptional())
from scout.mcp_service import main
sys.exit(main(['--config', sys.argv[1]]))
'''
            return subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True,
                                  text=True, env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src")))

    def test_disabled_has_no_sdk_or_provider_dependency(self):
        result = self.run_isolated('service = "invalid"\nagents = "invalid"\n[mcp]\nenabled = false\nport = "bad"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("disabled", result.stdout)

    def test_enabled_reports_missing_optional_runtime(self):
        result = self.run_isolated('''
[mcp]
enabled = true
bind_address = "10.20.30.40"
hostname = "scout.internal"
allowed_networks = ["10.80.0.0/16"]
''')
        self.assertEqual(result.returncode, 1)
        self.assertIn("scout-mcp-runtime", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_config_fails_with_actionable_message(self):
        result = self.run_isolated('[mcp]\nenabled = true\nbind_address = "0.0.0.0"\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn("mcp.bind_address", result.stderr)

    def test_uvicorn_never_trusts_proxy_headers(self):
        config = McpConfig(enabled=True, bind_address="10.20.30.40", hostname="scout.internal",
                           allowed_networks=("10.80.0.0/16",))
        app = object()
        run = Mock()
        with patch("scout.mcp_service.load_mcp_config", return_value=config), \
                patch("scout.mcp_service.create_app", return_value=app), \
                patch.dict(sys.modules, {"uvicorn": SimpleNamespace(run=run)}):
            self.assertEqual(main([]), 0)
        run.assert_called_once_with(app, host="10.20.30.40", port=8765, proxy_headers=False,
                                    access_log=False, limit_concurrency=32)

    def test_old_interpreter_has_actionable_runtime_error(self):
        with patch("scout.mcp_service.sys.version_info", (3, 9, 0)), self.assertRaisesRegex(RuntimeError, "Python 3.10"):
            create_app(McpConfig(enabled=True))


@unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK is not installed")
class McpHttpTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        self.config = McpConfig(enabled=True, bind_address="10.20.30.40", hostname="scout.internal",
                                allowed_networks=("10.80.0.0/16",))

        class DiagnosticsStub:
            def get_status(self):
                return {"available": True}

            def get_job(self, job_id):
                raise ValueError("secret value must not reach the client")

            def list_jobs(self, **arguments):
                return {"arguments": arguments}

            def read_logs(self, **arguments):
                # Backslashes double again inside the MCP text envelope.
                return {"text": "\\" * 12200, "truncated": True, "next_cursor": "next"}

        self.app = create_app(self.config, DiagnosticsStub())
        self.client = TestClient(self.app, base_url="http://scout.internal:8765", client=("10.80.2.3", 43210))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}

    def rpc(self, method, params=None, headers=None, request_id=1):
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post("/mcp", json=body, headers=dict(self.headers, **(headers or {})))

    def test_unauthenticated_initialize_lists_exact_read_only_tools(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("serverInfo", response.json()["result"])
        response = self.rpc("tools/list")
        self.assertEqual(response.status_code, 200, response.text)
        tools = response.json()["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools},
                         {"get_status", "list_jobs", "get_job", "read_logs", "read_run_output", "get_usage"})
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])

    def test_tool_result_is_single_json_text_and_arguments_reach_diagnostics(self):
        response = self.rpc("tools/call", {"name": "list_jobs", "arguments": {"pr_id": 123, "provider": "codex"}})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(len(result["content"]), 1)
        self.assertNotIn("structuredContent", result)
        self.assertEqual(json.loads(result["content"][0]["text"])["arguments"]["pr_id"], 123)

    def test_invalid_host_and_origin_are_rejected_by_sdk(self):
        self.assertEqual(self.rpc("tools/list", headers={"Host": "attacker.example:8765"}).status_code, 421)
        self.assertEqual(self.rpc("tools/list", headers={"Origin": "http://attacker.example"}).status_code, 403)
        self.assertEqual(self.rpc("tools/list", headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(self.rpc("tools/list", headers={"Origin": "http://scout.internal:8765"}).status_code, 200)

    def test_actual_source_is_enforced_across_every_http_route(self):
        from starlette.testclient import TestClient

        app = create_app(self.config, object())
        with TestClient(app, base_url="http://scout.internal:8765", client=("203.0.113.1", 1234)) as outside:
            for path in ("/mcp", "/", "/health"):
                response = outside.get(path, headers={"X-Forwarded-For": "10.80.2.3"})
                self.assertEqual(response.status_code, 403)

    def test_tool_error_does_not_reflect_internal_values(self):
        response = self.rpc("tools/call", {"name": "get_job", "arguments": {"job_id": 123}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["result"]["isError"])
        self.assertNotIn("secret value", response.text)
        self.assertIn("invalid diagnostic arguments", response.text)

    def test_full_http_reply_is_bounded_with_escaped_output_and_long_request_id(self):
        response = self.rpc("tools/call", {"name": "read_logs", "arguments": {}}, request_id="x" * 7000)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLessEqual(len(response.content), 65536)
        result = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), 24576)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_cursor"], "next")

    def test_sdk_rejects_large_request_bodies(self):
        response = self.rpc("tools/call", {"name": "get_status", "arguments": {}}, request_id="x" * 9000)
        self.assertEqual(response.status_code, 413, response.text)

    def test_real_log_reader_preserves_redaction_pagination_and_full_reply_bound(self):
        from starlette.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "daemon.log"
            original = (("token=super-secret-value " + "\\" * 300 + "\n") * 200).encode()
            log.write_bytes(original)
            config = replace(self.config, state_dir=tmp, state_db=str(Path(tmp) / "absent.db"), log_path=str(log))
            app = create_app(config)
            with TestClient(app, base_url="http://scout.internal:8765", client=("10.80.2.3", 43210)) as client:
                payload = {"jsonrpc": "2.0", "id": "x" * 7000, "method": "tools/call",
                           "params": {"name": "read_logs", "arguments": {}}}
                response = client.post("/mcp", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertLessEqual(len(response.content), 65536)
                self.assertNotIn("super-secret-value", response.text)
                result = json.loads(response.json()["result"]["content"][0]["text"])
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["truncated"])
                self.assertTrue(result["next_cursor"])
                self.assertGreater(len(result["records"]), 0)
                self.assertLess(len(result["records"]), 200)
                payload["params"]["arguments"]["cursor"] = result["next_cursor"]
                page = client.post("/mcp", json=payload, headers=self.headers)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertLessEqual(len(page.content), 65536)
                self.assertNotIn("super-secret-value", page.text)
            self.assertEqual(log.read_bytes(), original)
            self.assertFalse((Path(tmp) / "absent.db").exists())

    def test_http_reads_selected_retained_daemon_log_and_rejects_traversal(self):
        from starlette.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "daemon.log"
            rotation = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            log.write_text("current daemon log\n", encoding="utf-8")
            Path(str(log) + "." + rotation).write_text("retained startup failure\n", encoding="utf-8")
            config = replace(self.config, state_dir=tmp, state_db=str(Path(tmp) / "absent.db"), log_path=str(log))
            app = create_app(config)
            with TestClient(app, base_url="http://scout.internal:8765", client=("10.80.2.3", 43210)) as client:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "read_logs", "arguments": {"rotation": rotation}}}
                response = client.post("/mcp", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                result = json.loads(response.json()["result"]["content"][0]["text"])
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["records"], ["retained startup failure"])
                self.assertEqual(result["rotation"], rotation)
                self.assertEqual(result["available_rotations"], [rotation])
                payload["params"]["arguments"]["rotation"] = "../../credentials"
                response = client.post("/mcp", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                result = json.loads(response.json()["result"]["content"][0]["text"])
                self.assertEqual(result["status"], "invalid_request")
                self.assertNotIn("records", result)


if __name__ == "__main__":
    unittest.main()
