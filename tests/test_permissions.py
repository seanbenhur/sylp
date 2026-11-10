from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sylp_harness.permissions import (
    ALLOW,
    ASK,
    DENY,
    PermissionEngine,
    Rule,
    classify_command,
    patch_targets,
)


def engine(**kwargs) -> PermissionEngine:
    kwargs.setdefault("interactive", False)
    return PermissionEngine(**kwargs)


class ClassifyTests(unittest.TestCase):
    def test_read_only_commands_are_safe(self):
        for command in ("ls -la", "cat src/app.py", "git status", "git diff --stat",
                        "rg TODO .", "find . -name '*.py'"):
            self.assertEqual(classify_command(command), "safe", command)

    def test_git_write_subcommands_are_not_safe(self):
        for command in ("git push", "git commit -m x", "git", "git -c x=y status"):
            self.assertNotEqual(classify_command(command), "safe", command)

    def test_unknown_binary_is_simple(self):
        self.assertEqual(classify_command("make test"), "simple")
        self.assertEqual(classify_command("python -m pytest"), "simple")

    def test_compound_or_substituted_is_complex(self):
        for command in ("ls | grep x", "ls && rm -rf x", "echo $(date)", "cat `which x`",
                        "echo hi > f", "ls; pwd", "echo ${HOME}"):
            self.assertEqual(classify_command(command), "complex", command)


class PatchTargetTests(unittest.TestCase):
    def test_extracts_paths(self):
        patch = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                 "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-x\n+y\n")
        self.assertEqual(patch_targets(patch), ["src/a.py", ".git/config"])


class DefaultDecisionTests(unittest.TestCase):
    def test_read_only_tools_allow(self):
        e = engine()
        for tool, args in (("list_files", {"path": "."}), ("read_file", {"path": "a.py"}),
                           ("search_files", {"query": "x"}), ("git_status", {}), ("git_diff", {})):
            self.assertEqual(e.decide(tool, args).verdict, ALLOW, tool)

    def test_worktree_edits_allow(self):
        e = engine()
        self.assertEqual(e.decide("write_file", {"path": "src/a.py", "content": "x"}).verdict, ALLOW)
        self.assertEqual(e.decide("apply_patch", {"patch": "--- a/a.py\n+++ b/a.py\n"}).verdict, ALLOW)

    def test_git_and_sylp_are_protected(self):
        e = engine()
        self.assertEqual(e.decide("read_file", {"path": ".git/config"}).verdict, DENY)
        self.assertEqual(e.decide("list_files", {"path": ".git"}).verdict, DENY)
        self.assertEqual(e.decide("write_file", {"path": ".git/hooks/x", "content": "x"}).verdict, DENY)
        self.assertEqual(e.decide("write_file", {"path": ".sylp/plugins/evil.py", "content": "x"}).verdict, DENY)
        self.assertEqual(e.decide("apply_patch",
                                  {"patch": "--- a/.sylp/mcp.json\n+++ b/.sylp/mcp.json\n"}).verdict, DENY)

    def test_secret_files_ask_and_are_sensitive(self):
        e = engine()
        for tool, args in (("read_file", {"path": ".env"}),
                           ("read_file", {"path": "deploy/id_rsa"}),
                           ("write_file", {"path": "certs/server.pem", "content": "x"})):
            decision = e.decide(tool, args)
            self.assertEqual(decision.verdict, ASK, (tool, args))
            self.assertTrue(decision.sensitive, (tool, args))

    def test_run_command_defaults(self):
        e = engine()
        self.assertEqual(e.decide("run_command", {"command": "git status"}).verdict, ALLOW)
        self.assertEqual(e.decide("run_command", {"command": "make test"}).verdict, ASK)
        self.assertEqual(e.decide("run_command", {"command": "make test && deploy"}).verdict, ASK)

    def test_extension_tools_ask_and_are_sensitive(self):
        e = engine()
        decision = e.decide("mcp__filesystem__read", {})
        self.assertEqual(decision.verdict, ASK)
        self.assertTrue(decision.sensitive)
        self.assertTrue(e.decide("whoami", {}).sensitive)


class RuleTests(unittest.TestCase):
    def test_user_rule_allows_extension_tool(self):
        e = engine(user_rules=[Rule(decision=ALLOW, tool="mcp__fake__*", source="user")])
        self.assertEqual(e.decide("mcp__fake__echo", {}).verdict, ALLOW)
        self.assertEqual(e.decide("mcp__other__echo", {}).verdict, ASK)

    def test_user_command_prefix_rule(self):
        e = engine(user_rules=[Rule(decision=ALLOW, tool="run_command", command="make test",
                                    source="user")])
        self.assertEqual(e.decide("run_command", {"command": "make test"}).verdict, ALLOW)
        self.assertEqual(e.decide("run_command", {"command": "make test -k"}).verdict, ALLOW)
        self.assertEqual(e.decide("run_command", {"command": "make testcase"}).verdict, ASK)

    def test_repo_rules_can_only_tighten(self):
        # A repository cannot grant itself auto-approval...
        e = engine(repo_rules=[Rule(decision=ALLOW, tool="run_command", source="repo")])
        self.assertEqual(e.decide("run_command", {"command": "make"}).verdict, ASK)
        # ...but it can demand approval for a default-allow action.
        e = engine(repo_rules=[Rule(decision=ASK, tool="write_file", source="repo")])
        self.assertEqual(e.decide("write_file", {"path": "a.py", "content": "x"}).verdict, ASK)
        e = engine(repo_rules=[Rule(decision=DENY, tool="run_command", command="rm", source="repo")])
        self.assertEqual(e.decide("run_command", {"command": "rm -rf build"}).verdict, DENY)

    def test_repo_cannot_weaken_user_policy(self):
        user = [Rule(decision=ASK, tool="write_file", path="docs/**", source="user")]
        repo = [Rule(decision=ALLOW, tool="write_file", path="docs/**", source="repo")]
        e = engine(user_rules=user, repo_rules=repo)
        self.assertEqual(e.decide("write_file", {"path": "docs/x.md", "content": "x"}).verdict, ASK)

    def test_builtin_deny_beats_user_allow(self):
        e = engine(user_rules=[Rule(decision=ALLOW, tool="write_file", path=".git/**", source="user")])
        self.assertEqual(e.decide("write_file", {"path": ".git/config", "content": "x"}).verdict, DENY)

    def test_strictest_matching_rule_in_a_tier_wins(self):
        rules = [Rule(decision=ALLOW, tool="run_command", command="git", source="user"),
                 Rule(decision=DENY, tool="run_command", command="git push", source="user")]
        e = engine(user_rules=rules)
        self.assertEqual(e.decide("run_command", {"command": "git push origin"}).verdict, DENY)
        self.assertEqual(e.decide("run_command", {"command": "git fetch"}).verdict, ALLOW)


class CheckFlowTests(unittest.TestCase):
    def test_headless_ask_denies(self):
        e = engine()
        result = e.check("run_command", {"command": "make test"})
        self.assertIsNotNone(result)
        self.assertIn("cannot", result)

    def test_auto_approve_answers_routine_asks(self):
        e = engine(auto_approve=True)
        self.assertIsNone(e.check("run_command", {"command": "make test"}))

    def test_auto_approve_never_answers_sensitive_asks(self):
        e = engine(auto_approve=True)
        self.assertIsNotNone(e.check("mcp__fake__echo", {}))
        self.assertIsNotNone(e.check("read_file", {"path": ".env"}))

    def test_interactive_prompt_accepts_and_declines(self):
        e = engine(interactive=True, confirm=lambda prompt: True)
        self.assertIsNone(e.check("run_command", {"command": "make"}))
        e = engine(interactive=True, confirm=lambda prompt: False)
        self.assertIn("denied by the user", e.check("run_command", {"command": "make"}))

    def test_deny_message(self):
        e = engine()
        self.assertIn("Permission denied", e.check("write_file", {"path": ".git/config", "content": "x"}))

    def test_unsandboxed_needs_interactive_yes(self):
        self.assertFalse(engine(auto_approve=True).confirm_unsandboxed("make"))
        self.assertTrue(engine(interactive=True, confirm=lambda p: True).confirm_unsandboxed("make"))
        self.assertFalse(engine(interactive=True, confirm=lambda p: False).confirm_unsandboxed("make"))


class LoadTests(unittest.TestCase):
    def test_loads_user_and_repo_policy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "repo"
            (repo / ".sylp").mkdir(parents=True)
            home.mkdir()
            (home / "permissions.json").write_text(json.dumps({"rules": [
                {"tool": "mcp__fake__*", "decision": "allow"}]}), encoding="utf-8")
            (repo / ".sylp" / "permissions.json").write_text(json.dumps({"rules": [
                {"tool": "write_file", "decision": "ask"}]}), encoding="utf-8")
            e = PermissionEngine.load(home, repo, interactive=False)
            self.assertEqual(e.decide("mcp__fake__echo", {}).verdict, ALLOW)
            self.assertEqual(e.decide("write_file", {"path": "a.py", "content": "x"}).verdict, ASK)

    def test_invalid_rule_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "permissions.json").write_text(json.dumps({"rules": [
                {"tool": "*", "decision": "yolo"}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "allow"):
                PermissionEngine.load(home, None)


if __name__ == "__main__":
    unittest.main()
