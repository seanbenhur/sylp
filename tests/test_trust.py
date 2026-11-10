from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from sylp_harness import trust
from sylp_harness.extensions import ExtensionSet

from tests.test_mcp import FAKE_SERVER

PLUGIN = textwrap.dedent("""
    def _whoami(arguments, context):
        return "plugin-ok"

    def register():
        return [{"name": "whoami", "description": "Plugin probe", "handler": _whoami}]
""")


def make_repo(directory: Path, *, plugin: bool = True, mcp_server: bool = False) -> Path:
    repo = Path(directory) / "repo"
    if plugin:
        (repo / ".sylp" / "plugins").mkdir(parents=True, exist_ok=True)
        (repo / ".sylp" / "plugins" / "probe.py").write_text(PLUGIN, encoding="utf-8")
    if mcp_server:
        server = Path(directory) / "fake_server.py"
        server.write_text(FAKE_SERVER, encoding="utf-8")
        (repo / ".sylp").mkdir(parents=True, exist_ok=True)
        (repo / ".sylp" / "mcp.json").write_text(json.dumps({"servers": {
            "fake": {"command": sys.executable, "args": [str(server)]}}}), encoding="utf-8")
    repo.mkdir(exist_ok=True)
    return repo


class FingerprintTests(unittest.TestCase):
    def test_empty_repo_has_no_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.assertFalse(trust.has_extensions(trust.repo_fingerprint(repo)))

    def test_content_change_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(Path(directory))
            before = trust.repo_fingerprint(repo)
            (repo / ".sylp" / "plugins" / "probe.py").write_text(PLUGIN + "\n# touched\n",
                                                                 encoding="utf-8")
            self.assertNotEqual(before, trust.repo_fingerprint(repo))


class TrustGatingTests(unittest.TestCase):
    def test_untrusted_repo_extensions_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = make_repo(Path(directory), mcp_server=True)
            extensions = ExtensionSet.load(repo, env={"SYLP_HOME": str(home)})
            try:
                names = [schema["name"] for schema in extensions.schemas()]
                self.assertNotIn("whoami", names)
                self.assertNotIn("mcp__fake__echo", names)
                self.assertEqual(extensions.untrusted, [str(repo.resolve())])
                self.assertTrue(any("not trusted" in note for note in extensions.notices()))
                self.assertEqual(extensions.summary()["untrusted"], [str(repo.resolve())])
            finally:
                extensions.close()

    def test_confirm_true_trusts_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = make_repo(Path(directory))
            prompts: list[str] = []
            extensions = ExtensionSet.load(repo, env={"SYLP_HOME": str(home)},
                                           confirm=lambda prompt: prompts.append(prompt) or True)
            try:
                self.assertIn("whoami", [schema["name"] for schema in extensions.schemas()])
            finally:
                extensions.close()
            self.assertEqual(len(prompts), 1)
            self.assertIn("probe.py", prompts[0])
            # A later load needs no prompt: the recorded fingerprint matches.
            extensions = ExtensionSet.load(repo, env={"SYLP_HOME": str(home)})
            try:
                self.assertIn("whoami", [schema["name"] for schema in extensions.schemas()])
                self.assertEqual(extensions.untrusted, [])
            finally:
                extensions.close()

    def test_changed_content_asks_again(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = make_repo(Path(directory))
            store = trust.TrustStore(home)
            store.trust(repo, trust.repo_fingerprint(repo))
            (repo / ".sylp" / "plugins" / "probe.py").write_text(PLUGIN + "\n# changed\n",
                                                                 encoding="utf-8")
            extensions = ExtensionSet.load(repo, env={"SYLP_HOME": str(home)})
            try:
                self.assertNotIn("whoami", [schema["name"] for schema in extensions.schemas()])
            finally:
                extensions.close()

    def test_confirm_false_skips_without_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = make_repo(Path(directory))
            extensions = ExtensionSet.load(repo, env={"SYLP_HOME": str(home)},
                                           confirm=lambda prompt: False)
            try:
                self.assertEqual(extensions.untrusted, [str(repo.resolve())])
            finally:
                extensions.close()
            self.assertFalse((home / "trusted.json").exists())

    def test_home_extensions_need_no_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            (home / "plugins").mkdir(parents=True)
            (home / "plugins" / "probe.py").write_text(PLUGIN, encoding="utf-8")
            extensions = ExtensionSet.load(None, env={"SYLP_HOME": str(home)})
            try:
                self.assertIn("whoami", [schema["name"] for schema in extensions.schemas()])
            finally:
                extensions.close()


if __name__ == "__main__":
    unittest.main()
