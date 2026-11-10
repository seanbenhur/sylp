from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sylp_harness.sandbox import (
    SandboxUnavailable,
    policy_from_env,
    sandbox_env,
    scrubbed_env,
    wrap_command,
)
from sylp_harness.tools import execute_tool


class PolicyTests(unittest.TestCase):
    def test_defaults_to_auto_with_network_off(self):
        policy = policy_from_env({})
        self.assertEqual(policy.mode, "auto")
        self.assertFalse(policy.network)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            policy_from_env({"SYLP_SANDBOX": "fortress"})

    def test_network_on_variants(self):
        for value in ("1", "on", "true", "yes"):
            self.assertTrue(policy_from_env({"SYLP_SANDBOX_NETWORK": value}).network)

    def test_network_off_variants(self):
        for value in ("0", "off", "false", "no"):
            self.assertFalse(policy_from_env({"SYLP_SANDBOX_NETWORK": value}).network)


class ScrubTests(unittest.TestCase):
    def test_strips_secret_looking_names(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/u", "OPENAI_API_KEY": "sk-x",
               "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "MY_PASSWORD": "p",
               "NPM_CREDENTIALS": "c", "SSH_AUTH_SOCK": "/tmp/sock", "MONKEY": "ok"}
        scrubbed = scrubbed_env(env)
        self.assertEqual(scrubbed["PATH"], "/usr/bin")
        self.assertEqual(scrubbed["MONKEY"], "ok")
        self.assertEqual(scrubbed["SSH_AUTH_SOCK"], "/tmp/sock")
        for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                     "MY_PASSWORD", "NPM_CREDENTIALS"):
            self.assertNotIn(name, scrubbed)

    def test_env_allow_passes_named_variables(self):
        env = {"MY_TOKEN": "t", "SYLP_ENV_ALLOW": "MY_TOKEN"}
        self.assertEqual(scrubbed_env(env)["MY_TOKEN"], "t")


class WrapTests(unittest.TestCase):
    def test_off_mode_returns_command_unchanged(self):
        policy = policy_from_env({"SYLP_SANDBOX": "off"})
        command, applied = wrap_command("rm -rf /", Path("/tmp"), policy)
        self.assertEqual(command, "rm -rf /")
        self.assertTrue(applied)

    def test_bwrap_confines_writes_to_worktree(self):
        policy = policy_from_env({"SYLP_SANDBOX_NETWORK": "on"})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            command, applied = wrap_command("make", Path(directory), policy)
        self.assertTrue(applied)
        self.assertIn("bwrap", command)
        self.assertIn(f"--bind {Path(directory).resolve()} {Path(directory).resolve()}", command)
        self.assertNotIn("--unshare-net", command)

    def test_bwrap_uses_private_scratch_as_tmp(self):
        policy = policy_from_env({})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            scratch = Path(directory) / "scratch"
            policy.scratch = scratch
            command, applied = wrap_command("make", Path(directory) / "wt", policy)
            self.assertTrue(applied)
            self.assertIn(f"--bind {scratch} /tmp", command)
            self.assertIn("--setenv TMPDIR /tmp", command)
            self.assertTrue(scratch.is_dir())
            self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)

    def test_bwrap_network_off_unshares_net(self):
        policy = policy_from_env({"SYLP_SANDBOX_NETWORK": "off"})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            command, _applied = wrap_command("make", Path(directory), policy)
        self.assertIn("--unshare-net", command)

    def test_sandbox_exec_profile_allows_worktree_writes(self):
        policy = policy_from_env({"SYLP_SANDBOX_NETWORK": "off"})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="sandbox-exec"), \
                patch("sylp_harness.sandbox._backend_works", return_value=True):
            command, applied = wrap_command("make", Path(directory), policy)
        self.assertTrue(applied)
        self.assertIn("sandbox-exec", command)
        self.assertIn(f'(subpath "{Path(directory).resolve()}")', command)
        self.assertIn("(deny network*)", command)

    def test_sandbox_exec_env_redirects_tmpdir_to_scratch(self):
        policy = policy_from_env({})
        with tempfile.TemporaryDirectory() as directory, \
                patch("sylp_harness.sandbox.detect_backend", return_value="sandbox-exec"):
            policy.scratch = Path(directory) / "scratch"
            self.assertEqual(sandbox_env(policy), {"TMPDIR": str(policy.scratch)})

    def test_require_without_backend_raises(self):
        policy = policy_from_env({"SYLP_SANDBOX": "require"})
        with patch("sylp_harness.sandbox.detect_backend", return_value=None):
            with self.assertRaises(SandboxUnavailable):
                wrap_command("make", Path("/tmp"), policy)

    def test_auto_without_backend_reports_not_applied(self):
        policy = policy_from_env({"SYLP_SANDBOX": "auto"})
        with patch("sylp_harness.sandbox.detect_backend", return_value=None):
            command, applied = wrap_command("make", Path("/tmp"), policy)
        self.assertEqual(command, "make")
        self.assertFalse(applied)

    def test_broken_backend_probe_reports_not_applied(self):
        policy = policy_from_env({"SYLP_SANDBOX": "auto"})
        with patch("sylp_harness.sandbox.detect_backend", return_value="bwrap"), \
                patch("sylp_harness.sandbox._backend_works", return_value=False):
            command, applied = wrap_command("make", Path("/tmp"), policy)
        self.assertEqual(command, "make")
        self.assertFalse(applied)


class EnvScrubIntegrationTests(unittest.TestCase):
    def test_command_output_never_contains_host_secrets(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"SYLP_TEST_SECRET_TOKEN": "leakme"}, clear=False):
            out = execute_tool("run_command", {"command": "echo ${SYLP_TEST_SECRET_TOKEN:-unset}"},
                               Path(directory), auto_approve=True)
        self.assertIn("unset", out)
        self.assertNotIn("leakme", out)

    def test_env_allow_passthrough(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"SYLP_TEST_SECRET_TOKEN": "leakme",
                                        "SYLP_ENV_ALLOW": "SYLP_TEST_SECRET_TOKEN"}):
            out = execute_tool("run_command", {"command": "echo $SYLP_TEST_SECRET_TOKEN"},
                               Path(directory), auto_approve=True)
        self.assertIn("leakme", out)


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

    def test_tmp_is_a_private_scratch_not_shared_host_tmp(self):
        policy = policy_from_env({"SYLP_SANDBOX": "require"})
        with tempfile.TemporaryDirectory() as directory:
            policy.scratch = Path(directory) / "scratch"
            probe = "sylp-scratch-probe.txt"
            out = execute_tool("run_command",
                               {"command": f"echo hi > /tmp/{probe} && cat /tmp/{probe}"},
                               Path(directory), auto_approve=True, sandbox=policy)
            self.assertIn("hi", out)
            self.assertFalse(Path(f"/tmp/{probe}").exists(),
                             "sandboxed writes to /tmp must not reach the shared host /tmp")
            self.assertTrue((policy.scratch / probe).exists())


if __name__ == "__main__":
    unittest.main()
