"""Trust bootstrap for repository-supplied extensions.

Plugins and MCP servers configured by the repository (``.sylp/plugins/`` and
``.sylp/mcp.json``) run with Sylp's full privileges, so they load only after
the user explicitly trusts them. Trust is recorded in
``~/.sylp/trusted.json`` keyed by the resolved repository path and the exact
content hashes of the extension points; any change to a plugin file or the
MCP config asks again. Files under the user's own state home
(``~/.sylp/plugins/``, ``~/.sylp/mcp.json``) are already user-controlled and
load without prompting. Non-interactive sessions cannot prompt, so untrusted
repository extensions are skipped with a notice.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Callable

Confirm = Callable[[str], bool]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repo_fingerprint(repo: Path) -> dict:
    """Content hashes of the extension points a repository can supply."""
    repo = Path(repo).resolve()
    plugins: dict[str, str] = {}
    plugin_dir = repo / ".sylp" / "plugins"
    if plugin_dir.is_dir():
        for path in sorted(plugin_dir.glob("*.py")):
            if not path.name.startswith("_"):
                plugins[path.name] = _sha256(path.read_bytes())
    mcp_file = repo / ".sylp" / "mcp.json"
    return {
        "plugins": plugins,
        "mcp": _sha256(mcp_file.read_bytes()) if mcp_file.is_file() else None,
    }


def has_extensions(fingerprint: dict) -> bool:
    return bool(fingerprint["plugins"] or fingerprint["mcp"])


def describe(repo: Path, fingerprint: dict) -> str:
    lines = [f"\nThis repository ({Path(repo).resolve()}) provides Sylp extensions:"]
    for name in fingerprint["plugins"]:
        lines.append(f"  plugin: .sylp/plugins/{name}")
    if fingerprint["mcp"]:
        try:
            raw = json.loads((Path(repo).resolve() / ".sylp" / "mcp.json").read_text(encoding="utf-8"))
            servers = raw.get("servers", raw)
            for name, spec in servers.items():
                command = " ".join([str(spec.get("command", "?")),
                                    *[str(a) for a in spec.get("args", [])]])
                lines.append(f"  MCP server {name}: {command}")
        except Exception:
            lines.append("  MCP config: .sylp/mcp.json")
    lines.append("Extensions run with Sylp's full privileges (no approval prompts, no "
                 "sandbox). Trust them for this repository?")
    return "\n".join(lines)


class TrustStore:
    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "trusted.json"

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw.get("repos", {}) if isinstance(raw, dict) else {}

    def is_trusted(self, repo: Path, fingerprint: dict) -> bool:
        return self._read().get(str(Path(repo).resolve())) == fingerprint

    def trust(self, repo: Path, fingerprint: dict) -> None:
        repos = self._read()
        repos[str(Path(repo).resolve())] = fingerprint
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"repos": repos}, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


def ensure_repo_trusted(home: Path, repo: Path, *, confirm: Confirm | None = None) -> bool:
    """True when the repository's extensions may load. Records trust on approval."""
    fingerprint = repo_fingerprint(repo)
    if not has_extensions(fingerprint):
        return True
    store = TrustStore(Path(home))
    if store.is_trusted(repo, fingerprint):
        return True
    if confirm is None and sys.stdin.isatty():
        from .permissions import terminal_confirm
        confirm = terminal_confirm
    if confirm is None or not confirm(describe(repo, fingerprint)):
        return False
    store.trust(repo, fingerprint)
    return True
