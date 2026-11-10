"""Deterministic permission engine: every tool call is allow/ask/deny before it runs.

No model output participates in these decisions. Rules are exact, typed
entries loaded from two JSON files:

- ``~/.sylp/permissions.json`` - the user's policy. It decides whenever one of
  its rules matches, and nothing below it can weaken it.
- ``<repo>/.sylp/permissions.json`` - the repository's policy. It can only
  make the built-in defaults *stricter* (allow -> ask -> deny); a repository
  can never grant itself more than the defaults allow.

Built-in protections (deny writes to .git and .sylp, deny tool reads inside
.git) sit above both files and cannot be overridden. Anything left over falls
to per-tool defaults: read-only tools and workspace file edits are allowed,
secret-looking files ask, shell commands are parsed and only known read-only
forms auto-approve, and extension (MCP/plugin) tools ask.

File format:

{
  "rules": [
    {"tool": "run_command", "command": "git status", "decision": "allow"},
    {"tool": "mcp__filesystem__*", "decision": "ask"},
    {"tool": "write_file", "path": "docs/**", "decision": "allow"},
    {"tool": "*", "decision": "deny", "reason": "lock the harness down"}
  ]
}

``tool`` is a glob matched against the tool name. ``command`` (run_command
only) is a prefix match against the shell command string. ``path`` is a glob
matched against the worktree-relative path or its basename.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ALLOW, ASK, DENY = "allow", "ask", "deny"
_STRICTNESS = {ALLOW: 0, ASK: 1, DENY: 2}

Confirm = Callable[[str], bool]

READ_TOOLS = {"list_files", "read_file", "search_files", "git_status", "git_diff"}
WRITE_TOOLS = {"write_file", "apply_patch"}

# Writes to these top-level entries are always denied: .git holds the
# repository's integrity and credentials helpers, .sylp holds policy and
# executable extension points (plugins, MCP config). Reads inside .git are
# denied too; the rest of the worktree stays readable.
_DENY_WRITE_ROOTS = (".git", ".sylp")
_DENY_READ_ROOTS = (".git",)

# Basenames that usually hold credentials. Reading or writing them through a
# tool asks, and the ask is sensitive: auto-approve never answers it.
_SECRET_NAME_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc", "credentials",
)

# Shell command classification: only a single simple argv whose program is a
# known read-only binary auto-approves. Anything compound or unknown asks.
_OPERATOR_TOKENS = {
    "|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "<<<",
    "2>", "2>>", "&>", "&>>", "|&", "(", ")", "{", "}",
}
_SAFE_BINARIES = {
    "ls", "cat", "head", "tail", "wc", "file", "stat", "pwd", "echo",
    "printf", "find", "rg", "grep", "tree", "which", "basename", "dirname",
    "realpath", "date", "uname", "sort", "uniq", "diff",
}
_GIT_READ_ONLY = {
    "status", "diff", "log", "show", "branch", "rev-parse", "ls-files",
    "ls-tree", "blame", "describe", "shortlog",
}


def terminal_confirm(prompt: str) -> bool:
    """Default interactive approval prompt. Denies when stdin is unusable."""
    try:
        answer = input(prompt + "\n[y/N] ").strip().lower()
    except (EOFError, OSError):
        return False
    if answer in {"/exit", "/quit"}:
        raise KeyboardInterrupt
    return answer in {"y", "yes"}


def _norm_path(path: str) -> str:
    path = path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/") or "."


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _path_matches(path: str, pattern: str) -> bool:
    path = _norm_path(path)
    pattern = _norm_path(pattern)
    if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(Path(path).name, pattern):
        return True
    if not any(ch in pattern for ch in "*?["):
        return _under(path, pattern)
    return False


def _is_secret_path(path: str) -> bool:
    name = Path(_norm_path(path)).name
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_NAME_PATTERNS)


def patch_targets(patch: str) -> list[str]:
    """Worktree-relative paths a unified diff touches."""
    targets = []
    for line in patch.splitlines():
        if line.startswith(("+++ ", "--- ")):
            name = line[4:].strip()
            if name == "/dev/null":
                continue
            if name.startswith(("a/", "b/")):
                name = name[2:]
            if name not in targets:
                targets.append(name)
    return targets


def classify_command(command: str) -> str:
    """'safe' (read-only simple argv), 'simple' (single unknown argv), or
    'complex' (shell operators, substitutions, or unparseable)."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "complex"
    if not tokens:
        return "complex"
    for token in tokens:
        if token in _OPERATOR_TOKENS or "`" in token or "$(" in token or "${" in token:
            return "complex"
        # shlex does not split unquoted metacharacters attached to a word
        # ("ls;", "hi>f"), so flag them too; quoted literals over-ask, which
        # is the safe direction.
        if any(ch in token for ch in ";&|><\n"):
            return "complex"
    program = os.path.basename(tokens[0])
    if program in _SAFE_BINARIES:
        return "safe"
    if program == "git" and len(tokens) > 1 and tokens[1] in _GIT_READ_ONLY:
        return "safe"
    return "simple"


@dataclass
class Rule:
    decision: str
    tool: str = "*"
    command: str | None = None
    path: str | None = None
    source: str = "user"
    reason: str = ""

    def matches(self, tool: str, *, command: str | None, paths: list[str]) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        if self.command is not None:
            if command is None:
                return False
            prefix = self.command.strip()
            if command != prefix and not command.startswith(prefix + " "):
                return False
        if self.path is not None and not any(_path_matches(p, self.path) for p in paths):
            return False
        return True

    def describe(self) -> str:
        parts = [self.decision, self.tool]
        if self.command is not None:
            parts.append(f"command={self.command!r}")
        if self.path is not None:
            parts.append(f"path={self.path!r}")
        return " ".join(parts)


@dataclass
class Decision:
    verdict: str
    reason: str
    sensitive: bool = False  # never auto-approved, even with --auto-approve


def _load_rules(path: Path, source: str) -> list[Rule]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError(f"Permissions file {path} must contain a \"rules\" list")
    rules = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Permissions rule #{index + 1} in {path} must be an object")
        decision = entry.get("decision")
        if decision not in _STRICTNESS:
            raise ValueError(
                f"Permissions rule #{index + 1} in {path} needs decision allow|ask|deny")
        rules.append(Rule(
            decision=decision,
            tool=str(entry.get("tool") or "*"),
            command=entry.get("command"),
            path=entry.get("path"),
            source=source,
            reason=str(entry.get("reason") or ""),
        ))
    return rules


def _subject(tool: str, arguments: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The (command, paths) a rule matches against for one tool call."""
    if tool == "run_command":
        return str(arguments.get("command") or "").strip(), []
    if tool in {"read_file", "write_file", "list_files", "search_files"}:
        return None, [str(arguments.get("path") or ".")]
    if tool == "apply_patch":
        return None, patch_targets(str(arguments.get("patch") or ""))
    return None, []


class PermissionEngine:
    """Allow/ask/deny for every tool call, before execution."""

    def __init__(self, *, auto_approve: bool = False, interactive: bool | None = None,
                 user_rules: list[Rule] | None = None, repo_rules: list[Rule] | None = None,
                 confirm: Confirm | None = None) -> None:
        self.auto_approve = auto_approve
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.user_rules = list(user_rules or [])
        self.repo_rules = list(repo_rules or [])
        self._confirm = confirm or terminal_confirm

    @classmethod
    def load(cls, home: Path | None = None, repo: Path | None = None, *,
             auto_approve: bool = False, interactive: bool | None = None,
             confirm: Confirm | None = None) -> "PermissionEngine":
        user_rules = _load_rules(Path(home) / "permissions.json", "user") if home else []
        repo_rules = _load_rules(Path(repo) / ".sylp" / "permissions.json", "repo") if repo else []
        return cls(auto_approve=auto_approve, interactive=interactive,
                   user_rules=user_rules, repo_rules=repo_rules, confirm=confirm)

    def decide(self, tool: str, arguments: dict[str, Any]) -> Decision:
        command, paths = _subject(tool, arguments)

        # Built-in protections: above every policy file.
        if tool in WRITE_TOOLS:
            for path in paths:
                if any(_under(_norm_path(path), root) for root in _DENY_WRITE_ROOTS):
                    return Decision(DENY, f"{path} is protected: tools cannot write .git or .sylp "
                                          "(Sylp policy and extension points change outside the model)")
        if tool in READ_TOOLS:
            for path in paths:
                if any(_under(_norm_path(path), root) for root in _DENY_READ_ROOTS):
                    return Decision(DENY, f"{path} is protected: tools cannot read inside .git")

        # User policy decides next and cannot be weakened by the repository.
        matched = self._match(self.user_rules, tool, command, paths)
        if matched is not None:
            return matched

        default = self._default(tool, command, paths)

        # Repository policy can only tighten the default, never loosen it.
        matched = self._match(self.repo_rules, tool, command, paths)
        if matched is not None and _STRICTNESS[matched.verdict] > _STRICTNESS[default.verdict]:
            return matched
        return default

    def _match(self, rules: list[Rule], tool: str, command: str | None,
               paths: list[str]) -> Decision | None:
        best: Decision | None = None
        for rule in rules:
            if not rule.matches(tool, command=command, paths=paths):
                continue
            if best is None or _STRICTNESS[rule.decision] > _STRICTNESS[best.verdict]:
                best = Decision(rule.decision,
                                rule.reason or f"{rule.source} rule: {rule.describe()}")
        return best

    def _default(self, tool: str, command: str | None, paths: list[str]) -> Decision:
        if tool in READ_TOOLS:
            for path in paths:
                if _is_secret_path(path):
                    return Decision(ASK, f"{path} looks like a credentials file", sensitive=True)
            return Decision(ALLOW, "read-only tool")
        if tool in WRITE_TOOLS:
            for path in paths:
                if _is_secret_path(path):
                    return Decision(ASK, f"{path} looks like a credentials file", sensitive=True)
            return Decision(ALLOW, "edit inside the task worktree")
        if tool == "run_command":
            level = classify_command(command or "")
            if level == "safe":
                return Decision(ALLOW, "known read-only command")
            if level == "simple":
                return Decision(ASK, "command is not a known read-only form")
            return Decision(ASK, "compound or substituted shell command")
        return Decision(ASK, f"extension tool {tool} runs with Sylp's privileges", sensitive=True)

    def check(self, tool: str, arguments: dict[str, Any], *, prompt: str | None = None) -> str | None:
        """None to proceed, or the denial message to return as the tool result."""
        decision = self.decide(tool, arguments)
        if decision.verdict is DENY:
            return f"Permission denied: {decision.reason}"
        if decision.verdict is ALLOW:
            return None
        if self.auto_approve and not decision.sensitive:
            return None
        if not self.interactive:
            return (f"{tool} needs approval ({decision.reason}), but this session cannot "
                    "prompt. Ask the user to approve interactively, or add an allow rule "
                    "to permissions.json.")
        if self._confirm(prompt or f"\nAllow {tool}? ({decision.reason})"):
            return None
        return f"{tool} denied by the user. Continue with other tools or ask for guidance."

    def confirm_unsandboxed(self, command: str) -> bool:
        """Unsandboxed execution is always a sensitive, explicit choice: never
        auto-approved, never answered headless."""
        if not self.interactive:
            return False
        return self._confirm(
            f"\nNo working sandbox backend is available.\n  {command}\n"
            "Run WITHOUT sandboxing (full host access)?")
