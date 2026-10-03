import io
import json
import tempfile
import unittest
from repvblicvs_engine.mcp import Server
from repvblicvs_engine.store import Store


class McpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.server = Server(self.store)

    def request(self, method, params=None, request_id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

    def test_initialize_tools_and_unknown_method(self):
        reply = self.request("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        tools = self.request("tools/list")["result"]["tools"]
        self.assertIn("engine_priority", {tool["name"] for tool in tools})
        self.assertIn("engine_reserve_provider", {tool["name"] for tool in tools})
        self.assertEqual(self.request("invalid")["error"]["code"], -32601)

    def test_frontends_share_idempotent_submission_and_control(self):
        arguments = {"spec": {"kind": "test", "payload": {}}, "request_id": "shared-request"}
        first = self.request("tools/call", {"name": "engine_submit", "arguments": arguments})
        other = Server(Store(self.directory.name))
        second = other.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "engine_submit", "arguments": arguments}})
        self.assertEqual(first["result"]["structuredContent"]["id"], second["result"]["structuredContent"]["id"])
        other.call_tool("engine_control", {"mode": "paused"})
        self.assertEqual(self.server.call_tool("engine_status", {})["control"], "paused")
        task_id = first["result"]["structuredContent"]["id"]
        self.server.call_tool("engine_priority", {"task_id": task_id, "priority": 99})
        self.assertEqual(other.call_tool("engine_tasks", {"task_id": task_id})["priority"], 99)

    def test_artifact_paths_cannot_escape(self):
        target = self.store.artifact_root / "safe.txt"
        target.write_text("fixture")
        self.assertEqual(self.server.call_tool("engine_read_artifact", {"path": "safe.txt"})["text"], "fixture")
        reply = self.request("tools/call", {"name": "engine_read_artifact", "arguments": {"path": "../engine.sqlite3"}})
        self.assertTrue(reply["result"]["isError"])
        target.unlink()
        target.symlink_to(self.store.path)
        with self.assertRaises(ValueError):
            self.server.call_tool("engine_read_artifact", {"path": "safe.txt"})

    def test_tool_invalid_arguments_are_errors(self):
        for arguments in ({"mode": "invalid"}, {"mode": "paused", "extra": True}, {}):
            reply = self.request("tools/call", {"name": "engine_control", "arguments": arguments})
            self.assertTrue(reply["result"]["isError"])
        reply = self.request("tools/call", {"name": "engine_events", "arguments": {"after_id": True}})
        self.assertTrue(reply["result"]["isError"])

    def test_transport_parse_error_notification_and_continuation(self):
        incoming = io.StringIO("invalid\n" + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
        outgoing = io.StringIO()
        self.server.serve(incoming, outgoing)
        messages = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["error"]["code"], -32700)
        self.assertEqual(messages[1]["result"], {})


if __name__ == "__main__":
    unittest.main()
