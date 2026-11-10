"""Combined view of MCP servers and local plugins for one task run."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import mcp
from .plugins import PluginRegistry


def state_home(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get("SYLP_HOME") or env.get("EVERUN_HOME") or env.get("RELAY_HOME")
    if configured:
        return Path(configured)
    return Path.home() / ".sylp"


class ExtensionSet:
    def __init__(self, manager: mcp.MCPManager, plugins: PluginRegistry) -> None:
        self.manager = manager
        self.plugins = plugins

    @classmethod
    def load(cls, repo: Path | None, *, env: dict[str, str] | None = None) -> "ExtensionSet":
        home = state_home(env)
        config = mcp.load_config(home, repo, env)
        manager = mcp.MCPManager.start(config) if config else mcp.MCPManager()
        plugins = PluginRegistry().load(home, repo)
        return cls(manager, plugins)

    def schemas(self) -> list[dict[str, Any]]:
        return self.manager.schemas() + self.plugins.schemas()

    def handles(self, name: str) -> bool:
        return self.manager.handles(name) or self.plugins.handles(name)

    def call(self, name: str, arguments: dict[str, Any], root: Path) -> str:
        if self.manager.handles(name):
            return self.manager.call(name, arguments)
        return self.plugins.call(name, arguments, root)

    def notices(self) -> list[str]:
        notes = [f"MCP server {entry} failed to start" for entry in self.manager.errors]
        notes.extend(f"plugin skipped ({entry})" for entry in self.plugins.errors)
        return notes

    def summary(self) -> dict[str, Any]:
        return {
            "mcp_servers": sorted(self.manager.servers),
            "mcp_tools": len(self.manager.schemas()),
            "plugin_files": [str(p) for p in self.plugins.loaded],
            "plugin_tools": len(self.plugins.schemas()),
            "errors": self.manager.errors + self.plugins.errors,
        }

    def close(self) -> None:
        self.manager.close()
