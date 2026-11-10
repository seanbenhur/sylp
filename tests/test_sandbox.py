from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness.sandbox import SandboxUnavailable, policy_from_env, wrap_command
from sylp_harness.tools import execute_tool


class PolicyTests(unittest.TestCase):
    def test_defaults_to_auto_with_network(self):
        policy = policy_from_env({})
        self.assertEqual(policy.mode, "auto")
        self.assertTrue(policy.network)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            policy_from_env({"SYLP_SANDBOX": "fortress"})

    def test_network_off_variants(self):
        for value in ("0", "off", "false", "no"):
            self.assertFalse(policy_from_env({"SYLP_SANDBOX_NETWORK": value}).network)


class WrapTests(unittest.TestCase):
    def test_off_mode_returns_command_unchanged(self):
        policy = policy_from_env({"SYLP_SANDBOX": "off"})
        command, note = wrap_command("rm -rf /", Path("/tmp"), policy)
        self.assertEqual(command, "rm -rf /")
        self.assertIsNone(note)

    def test_bwrap_confines_writes_to_worktree(self):
        policy = policy_from_env({})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            command, note = wrap_command("make", Path(directory), policy)
        self.assertIsNone(note)
        self.assertIn("bwrap", command)
        self.assertIn(f"--bind {directory} {directory}", command)
        self.assertNotIn("--unshare-net", command)

    def test_bwrap_network_off_unshares_net(self):
        policy = policy_from_env({"SYLP_SANDBOX_NETWORK": "off"})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            command, _note = wrap_command("make", Path(directory), policy)
        self.assertIn("--unshare-net", command)

    def test_sandbox_exec_profile_allows_worktree_writes(self):
        policy = policy_from_env({"SYLP_SANDBOX_NETWORK": "off"})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="sandbox-exec"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            command, note = wrap_command("make", Path(directory), policy)
        self.assertIsNone(note)
        self.assertIn("sandbox-exec", command)
        self.assertIn(f'(subpath "{Path(directory).resolve()}")', command)
        self.assertIn("(deny network*)", command)

    def test_require_without_backend_raises(self):
        policy = policy_from_env({"SYLP_SANDBOX": "require"})
        with patch("sylp_harness.sandbox.detect_backend", return_value=None):
            with self.assertRaises(SandboxUnavailable):
                wrap_command("make", Path("/tmp"), policy)

    def test_auto_without_backend_warns_and_runs_unwrapped(self):
        policy = policy_from_env({"SYLP_SANDBOX": "auto"})
        with patch("sylp_harness.sandbox.detect_backend", return_value=None):
            command, note = wrap_command("make", Path("/tmp"), policy)
        self.assertEqual(command, "make")
        self.assertIn("WITHOUT sandboxing", note)

    def test_broken_backend_probe_degrades_in_auto_mode(self):
        policy = policy_from_env({"SYLP_SANDBOX": "auto"})
        with patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=False):
            command, note = wrap_command("make", Path("/tmp"), policy)
        self.assertEqual(command, "make")
        self.assertIn("WITHOUT sandboxing", note)


@unittest.skipUnless(__import__("shutil").which("bwrap"), "needs bubblewrap")
class RealSandboxTests(unittest.TestCase):
    def test_worktree_writable_and_system_read_only(self):
        policy = policy_from_env({"SYLP_SANDBOX": "require"})
        with tempfile.TemporaryDirectory() as directory:
            out = execute_tool("run_command", {"command": "echo ok > f.txt && cat f.txt"},
                               Path(directory), auto_approve=True, sandbox=policy)
            self.assertIn("ok", out)
            out = execute_tool("run_command", {"command": "touch /usr/bin/nope || echo blocked"},
                               Path(directory), auto_approve=True, sandbox=policy)
            self.assertIn("blocked", out)

    def test_network_off_blocks_network(self):
        policy = policy_from_env({"SYLP_SANDBOX": "require", "SYLP_SANDBOX_NETWORK": "off"})
        with tempfile.TemporaryDirectory() as directory:
            out = execute_tool("run_command",
                               {"command": "curl -sS -m 5 https://example.com || echo net-blocked"},
                               Path(directory), auto_approve=True, sandbox=policy)
            self.assertIn("net-blocked", out)


if __name__ == "__main__":
    unittest.main()
