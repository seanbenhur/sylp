"""Minimal Model Context Protocol (MCP) client over stdio.

Stdlib-only: newline-delimited JSON-RPC 2.0, which is what MCP stdio
servers speak. Servers are configured in mcp.json (see docs/extensions.md)
and started when a task begins; their tools reach the model with names
qualified as mcp__<server>__<tool>.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
INIT_TIMEOUT_SECONDS = 30
CALL_TIMEOUT_SECONDS = 120
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def load_config(home: Path | None = None, repo: Path | None = None,
                env: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Merge mcp.json files; later sources win on a server-name conflict."""
    env = os.environ if env is None else env
    candidates: list[Path] = []
    override = env.get("SYLP_MCP_CONFIG")
    if override:
        candidates.append(Path(override))
    else:
        if home is not None:
            candidates.append(Path(home) / "mcp.json")
        if repo is not None:
            candidates.append(Path(repo) / ".sylp" / "mcp.json")
    servers: dict[str, dict[str, Any]] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in MCP config {path}: {exc}") from exc
        entries = raw.get("servers", raw)
        if not isinstance(entries, dict):
            raise ValueError(f"MCP config {path} must contain a \"servers\" object")
        for name, spec in entries.items():
            if not isinstance(spec, dict) or not spec.get("command"):
                raise ValueError(f"MCP server {name!r} in {path} needs a \"command\"")
            servers[str(name)] = spec
    return servers


def qualify(server: str, tool: str) -> str:
    name = f"mcp__{_NAME_SAFE.sub('_', server)}__{_NAME_SAFE.sub('_', tool)}"
    return name[:64]


class MCPServer:
    """One running MCP stdio server."""

    def __init__(self, name: str, spec: dict[str, Any]):
        self.name = name
        self.spec = spec
        self.process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        # Servers get a secret-scrubbed environment; credentials a server
        # needs belong in its explicit "env" config block.
        from .sandbox import scrubbed_env
        env = scrubbed_env()
        for key, value in (self.spec.get("env") or {}).items():
            env[str(key)] = str(value)
        argv = [str(self.spec["command"]), *[str(a) for a in self.spec.get("args", [])]]
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=self.spec.get("cwd") or None, env=env,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sylp", "version": "0.1.0"},
        }, timeout=INIT_TIMEOUT_SECONDS)
        self._notify("notifications/initialized", {})

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._responses.put(message)

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any], *, timeout: int) -> Any:
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError(f"MCP server {self.name!r} is not running")
            request_id = next(self._ids)
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = timeout
            while True:
                try:
                    message = self._responses.get(timeout=deadline)
                except queue.Empty as exc:
                    raise RuntimeError(f"MCP server {self.name!r} timed out on {method}") from exc
                if message.get("id") != request_id:
                    continue  # a notification or another in-flight reply
                if "error" in message:
                    detail = message["error"]
                    raise RuntimeError(f"MCP server {self.name!r} rejected {method}: {detail.get('message', detail)}")
                return message.get("result", {})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except (BrokenPipeError, OSError):
            pass

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {}, timeout=INIT_TIMEOUT_SECONDS)
        tools = result.get("tools", [])
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> str:
        timeout = int(self.spec.get("timeout_seconds", CALL_TIMEOUT_SECONDS))
        result = self._request("tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)
        parts: list[str] = []
        for item in result.get("content", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        text = "\n".join(part for part in parts if part).strip() or "(no content returned)"
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {self.name}/{tool} reported an error: {text}")
        return text

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except OSError:
                pass


class MCPManager:
    """The set of MCP servers attached to one task run."""

    def __init__(self) -> None:
        self.servers: dict[str, MCPServer] = {}
        self.errors: list[str] = []
        self._tools: dict[str, tuple[MCPServer, str, dict[str, Any]]] = {}

    @classmethod
    def start(cls, config: dict[str, dict[str, Any]]) -> "MCPManager":
        manager = cls()
        for name, spec in config.items():
            server = MCPServer(name, spec)
            try:
                server.start()
                tools = server.list_tools()
            except Exception as exc:
                server.close()
                manager.errors.append(f"{name}: {exc}")
                continue
            manager.servers[name] = server
            for tool in tools:
                manager._tools[qualify(name, str(tool["name"]))] = (server, str(tool["name"]), tool)
        return manager

    def schemas(self) -> list[dict[str, Any]]:
        result = []
        for qualified, (server, _original, tool) in sorted(self._tools.items()):
            schema = tool.get("inputSchema")
            result.append({
                "name": qualified,
                "description": f"[MCP {server.name}] {tool.get('description') or qualified}",
                "parameters": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            })
        return result

    def handles(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        server, original, _tool = self._tools[name]
        return server.call_tool(original, arguments)

    def close(self) -> None:
        for server in self.servers.values():
            server.close()
