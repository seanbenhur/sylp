from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness import runner
from sylp_harness.extensions import ExtensionSet
from sylp_harness.git import prepare_worktree
from sylp_harness.store import Store
from tests.test_mcp import FAKE_SERVER

PLUGIN = textwrap.dedent("""
    def _whoami(arguments, context):
        return "plugin-ok"

    def register():
        return [{"name": "whoami", "description": "Plugin probe", "handler": _whoami}]
""")


class ExtensionSetTests(unittest.TestCase):
    def test_loads_mcp_and_plugins_together(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            (home / "plugins").mkdir(parents=True)
            (home / "plugins" / "probe.py").write_text(PLUGIN, encoding="utf-8")
            server = Path(directory) / "fake_server.py"
            server.write_text(FAKE_SERVER, encoding="utf-8")
            (home / "mcp.json").write_text(json.dumps({"servers": {
                "fake": {"command": sys.executable, "args": [str(server)]}}}), encoding="utf-8")
            with patch.dict(os.environ, {"SYLP_HOME": str(home)}):
                extensions = ExtensionSet.load(None)
            try:
                names = [schema["name"] for schema in extensions.schemas()]
                self.assertIn("mcp__fake__echo", names)
                self.assertIn("whoami", names)
                self.assertEqual(extensions.call("whoami", {}, Path(directory)), "plugin-ok")
                self.assertEqual(extensions.call("mcp__fake__echo", {"text": "hi"}, Path(directory)), "echo: hi")
                summary = extensions.summary()
                self.assertEqual(summary["mcp_servers"], ["fake"])
                self.assertEqual(summary["errors"], [])
            finally:
                extensions.close()


class RunnerExtensionTests(unittest.TestCase):
    def test_model_can_call_mcp_and_plugin_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Sylp Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "sylp-test@example.invalid"], cwd=root, check=True)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)

            state = Path(directory) / "state"
            (state / "plugins").mkdir(parents=True)
            (state / "plugins" / "probe.py").write_text(PLUGIN, encoding="utf-8")
            server = Path(directory) / "fake_server.py"
            server.write_text(FAKE_SERVER, encoding="utf-8")
            (state / "mcp.json").write_text(json.dumps({"servers": {
                "fake": {"command": sys.executable, "args": [str(server)]}}}), encoding="utf-8")

            with patch.dict(os.environ, {"SYLP_HOME": str(state)}):
                worktree, base = prepare_worktree(root, "extensions1")
                store = Store()
                task_id = store.create(goal="Probe the extension tools and report.",
                                       acceptance=[], repo=root, worktree=worktree, base_commit=base,
                                       model="fake-model", provider="openai", auto_approve=True)
                seen_tools: list[list[str]] = []
                responses = iter([
                    {"text": "", "tool_calls": [
                        {"id": "c1", "name": "mcp__fake__echo", "arguments": {"text": "ping"}},
                        {"id": "c2", "name": "whoami", "arguments": {}},
                    ], "history_items": [
                        {"type": "function_call", "call_id": "c1", "name": "mcp__fake__echo",
                         "arguments": '{"text": "ping"}'},
                        {"type": "function_call", "call_id": "c2", "name": "whoami", "arguments": "{}"},
                    ], "response_id": "r1"},
                    {"text": "Both extension tools answered.", "tool_calls": [],
                     "history_items": [{"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text",
                                                     "text": "Both extension tools answered."}]}],
                     "response_id": "r2"},
                ])

                def fake_complete(provider, model, history, tools, system, **kwargs):
                    seen_tools.append([tool["name"] for tool in tools])
                    return next(responses)

                with patch.object(runner, "complete", side_effect=fake_complete), \
                        contextlib.redirect_stdout(io.StringIO()):
                    runner.run_task(task_id)

                events = store.events(task_id)
                messages = store.messages(task_id)
                final = store.get(task_id)
                store.close()

            self.assertEqual(final["status"], "review_required")
            self.assertIn("mcp__fake__echo", seen_tools[0])
            self.assertIn("whoami", seen_tools[0])
            outputs = [m["output"] for m in messages if m.get("type") == "function_call_output"]
            self.assertIn("echo: ping", outputs[0])
            self.assertIn("plugin-ok", outputs[1])
            loaded = next(e for e in events if e["kind"] == "extensions_loaded")
            payload = json.loads(loaded["payload"])
            self.assertEqual(payload["mcp_servers"], ["fake"])
            self.assertEqual(payload["plugin_tools"], 1)


if __name__ == "__main__":
    unittest.main()
