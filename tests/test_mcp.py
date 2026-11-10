from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from everrun_harness import mcp

FAKE_SERVER = textwrap.dedent("""
    import json
    import sys

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" not in msg:
            continue  # a notification, e.g. notifications/initialized
        method = msg["method"]
        if method == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"],
                      "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": "echo", "description": "Echo back text",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
                {"name": "fail", "description": "Always fails"},
            ]}
        elif method == "tools/call":
            if msg["params"]["name"] == "fail":
                result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
            else:
                result = {"content": [{"type": "text",
                                       "text": "echo: " + msg["params"]["arguments"].get("text", "")}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": "unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
""")


def fake_config(directory: Path) -> dict[str, dict]:
    server = Path(directory) / "fake_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    return {"fake": {"command": sys.executable, "args": [str(server)]}}


class ConfigTests(unittest.TestCase):
    def test_missing_files_yield_empty_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            self.assertEqual(mcp.load_config(home, repo, {}), {})

    def test_invalid_json_raises_with_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "mcp.json").write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mcp.json"):
                mcp.load_config(home, None, {})

    def test_repo_config_overrides_home_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            (repo / ".everrun").mkdir(parents=True)
            (home / ".everrun").mkdir(parents=True)
            home.mkdir(exist_ok=True)
            (home / "mcp.json").write_text(json.dumps({"servers": {
                "a": {"command": "from-home"}, "b": {"command": "home-b"}}}), encoding="utf-8")
            (repo / ".everrun" / "mcp.json").write_text(json.dumps({"servers": {
                "b": {"command": "repo-b"}}}), encoding="utf-8")
            config = mcp.load_config(home, repo, {})
            self.assertEqual(config["a"]["command"], "from-home")
            self.assertEqual(config["b"]["command"], "repo-b")

    def test_server_without_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "mcp.json").write_text(json.dumps({"servers": {"bad": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "command"):
                mcp.load_config(home, None, {})


class ServerTests(unittest.TestCase):
    def test_handshake_list_call_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = mcp.MCPManager.start(fake_config(Path(directory)))
            try:
                self.assertEqual(manager.errors, [])
                names = [schema["name"] for schema in manager.schemas()]
                self.assertIn("mcp__fake__echo", names)
                self.assertIn("mcp__fake__fail", names)
                echo = next(schema for schema in manager.schemas() if schema["name"] == "mcp__fake__echo")
                self.assertIn("[MCP fake]", echo["description"])
                self.assertEqual(echo["parameters"]["required"], ["text"])
                self.assertTrue(manager.handles("mcp__fake__echo"))
                self.assertEqual(manager.call("mcp__fake__echo", {"text": "hi"}), "echo: hi")
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    manager.call("mcp__fake__fail", {})
            finally:
                manager.close()
            self.assertNotEqual(manager.servers["fake"].process.poll(), None)

    def test_unstartable_server_is_recorded_not_raised(self):
        manager = mcp.MCPManager.start({"ghost": {"command": "definitely-not-a-real-binary-xyz"}})
        try:
            self.assertEqual(manager.servers, {})
            self.assertEqual(manager.schemas(), [])
            self.assertEqual(len(manager.errors), 1)
            self.assertIn("ghost", manager.errors[0])
        finally:
            manager.close()

    def test_qualify_sanitizes_and_truncates(self):
        self.assertEqual(mcp.qualify("my server", "do.thing"), "mcp__my_server__do_thing")
        self.assertLessEqual(len(mcp.qualify("x" * 100, "y" * 100)), 64)


if __name__ == "__main__":
    unittest.main()
