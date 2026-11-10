"""Local Everrun plugins: trusted Python files that add model tools.

A plugin is a .py file in ~/.everrun/plugins/ or <repo>/.everrun/plugins/
defining register() -> list of tool dicts. Plugins run in-process with
Everrun's full privileges; only install plugins you trust. See
docs/extensions.md for the contract.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Callable

from .tools import TOOL_SCHEMAS

_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_BUILTIN_NAMES = {schema["name"] for schema in TOOL_SCHEMAS}

ToolHandler = Callable[[dict[str, Any], dict[str, Any]], str]


def plugin_dirs(home: Path | None = None, repo: Path | None = None) -> list[Path]:
    dirs = []
    if home is not None:
        dirs.append(Path(home) / "plugins")
    if repo is not None:
        dirs.append(Path(repo) / ".everrun" / "plugins")
    return dirs


def discover(home: Path | None = None, repo: Path | None = None) -> list[Path]:
    files: list[Path] = []
    for directory in plugin_dirs(home, repo):
        if directory.is_dir():
            files.extend(sorted(p for p in directory.glob("*.py") if not p.name.startswith("_")))
    return files


class PluginRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolHandler, dict[str, Any]]] = {}
        self.errors: list[str] = []
        self.loaded: list[Path] = []

    def _add(self, path: Path, entry: dict[str, Any], reserved: set[str]) -> None:
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(f"tool name {name!r} must match {_NAME.pattern}")
        if name.startswith("mcp__") or name in reserved:
            raise ValueError(f"tool name {name!r} is already taken")
        handler = entry.get("handler")
        if not callable(handler):
            raise ValueError(f"tool {name!r} has no callable handler")
        parameters = entry.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError(f"tool {name!r} parameters must be a JSON schema object")
        reserved.add(name)
        self._tools[name] = (handler, {
            "name": name,
            "description": str(entry.get("description") or f"Plugin tool from {path.name}"),
            "parameters": parameters or {"type": "object", "properties": {}},
        })

    def load(self, home: Path | None = None, repo: Path | None = None) -> "PluginRegistry":
        reserved = set(_BUILTIN_NAMES)
        for path in discover(home, repo):
            module_name = f"everrun_plugin_{path.stem}_{abs(hash(path))}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ValueError("could not load module spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise ValueError("plugin defines no register() function")
                entries = register()
                if not isinstance(entries, list):
                    raise ValueError("register() must return a list of tool dicts")
                for entry in entries:
                    self._add(path, entry, reserved)
            except Exception as exc:
                self.errors.append(f"{path}: {exc}")
                continue
            self.loaded.append(path)
        return self

    def schemas(self) -> list[dict[str, Any]]:
        return [value[1] for _name, value in sorted(self._tools.items())]

    def handles(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, arguments: dict[str, Any], root: Path) -> str:
        handler, _schema = self._tools[name]
        result = handler(arguments, {"root": root})
        return "(plugin returned no output)" if result is None else str(result)
