from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness import cli


class InteractiveSetupTests(unittest.TestCase):
    def test_version_is_available_without_starting_a_session(self):
        executable = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "sylp"
        if not executable.exists():
            self.skipTest("editable Sylp console script is not installed in .venv")
        result = subprocess.run([str(executable), "--version"], text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^sylp \d+\.\d+\.\d+\n$")

    def test_interactive_shell_auto_approval_requires_explicit_environment_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for value, expected in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False)):
                with self.subTest(value=value), \
                        patch.dict(os.environ, {"SYLP_AUTO_APPROVE": value}), \
                        patch.object(cli, "_git_root", return_value=repo), \
                        patch.object(cli, "Store") as store_type, \
                        patch.object(cli, "is_connected", return_value=True), \
                        patch.object(cli, "default_model", return_value="test-model"), \
                        patch.object(cli, "default_provider", return_value="openai"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    store_type.return_value.list.return_value = []
                    args = cli._interactive()

                self.assertEqual(args.auto_approve, expected)

    def test_first_run_uses_the_only_connected_provider(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"SYLP_HOME": directory}, clear=True), \
                patch.object(cli, "_git_root", return_value=Path(directory)), \
                patch.object(cli, "Store") as store_type, \
                patch.object(cli, "is_connected", side_effect=lambda name: name == "anthropic"), \
                patch.object(cli, "default_provider", return_value=None), \
                patch.object(cli, "default_model", return_value=None), \
                patch.object(cli, "list_models", return_value=[{"id": "claude-test", "name": "Claude Test"}]), \
                patch.object(cli, "save_default_provider") as save_provider, \
                patch.object(cli, "save_default_model") as save_model, \
                patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            store_type.return_value.list.return_value = []
            args = cli._interactive()

        self.assertEqual((args.provider, args.model), ("anthropic", "claude-test"))
        save_provider.assert_called_once_with("anthropic")
        save_model.assert_called_once_with("anthropic", "claude-test")

    def test_first_run_prompts_when_multiple_providers_are_connected(self):
        connected = {"anthropic", "openrouter"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"SYLP_HOME": directory}, clear=True), \
                patch.object(cli, "_git_root", return_value=Path(directory)), \
                patch.object(cli, "Store") as store_type, \
                patch.object(cli, "is_connected", side_effect=lambda name: name in connected), \
                patch.object(cli, "default_provider", return_value=None), \
                patch.object(cli, "default_model", return_value=None), \
                patch.object(cli, "list_models", return_value=[{"id": "model-test", "name": "Model Test"}]), \
                patch.object(cli, "save_default_provider") as save_provider, \
                patch.object(cli, "save_default_model"), \
                patch("builtins.input", side_effect=["bogus", "openrouter", "model-test"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            store_type.return_value.list.return_value = []
            args = cli._interactive()

        self.assertEqual(args.provider, "openrouter")
        self.assertIn("Choose one of: anthropic, openrouter", output.getvalue())
        save_provider.assert_called_once_with("openrouter")


if __name__ == "__main__":
    unittest.main()
