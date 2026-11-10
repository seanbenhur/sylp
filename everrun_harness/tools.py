from __future__ import annotations

import codecs
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

TOOL_SCHEMAS = [
    {"name": "list_files", "description": "List repository files under a relative path.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative directory; defaults to the repository root."}}, "required": []}},
    {"name": "read_file", "description": "Read a UTF-8 text file in the repository.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create or replace a UTF-8 text file in the repository. Creates parent directories as needed.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "apply_patch", "description": "Apply a standard unified diff to files in the repository worktree.", "parameters": {"type": "object", "properties": {"patch": {"type": "string", "description": "Unified diff text, optionally with a/ and b/ path prefixes."}}, "required": ["patch"]}},
    {"name": "search_files", "description": "Search tracked and untracked text files in the repository for a literal or regular expression.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "description": "Optional relative file or directory."}}, "required": ["query"]}},
    {"name": "git_status", "description": "Show changed, staged, and untracked files in the task worktree.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "git_diff", "description": "Show the current tracked diff in the task worktree.", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_command", "description": "Run a shell command in the repository worktree. Commands can have side effects and may require user approval.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600}}, "required": ["command"]}},
]

_IGNORED = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
_MAX_OUTPUT = 24000


def _stop_process(process: subprocess.Popen[Any]) -> tuple[Any, Any]:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
        return process.communicate(timeout=3)
    except ProcessLookupError:
        return process.communicate()
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
    return process.communicate()


def _run_command(command: str, root: Path, timeout: int, on_output: Any | None,
                 on_checkpoint: Any | None = None) -> str:
    process = subprocess.Popen(command, cwd=root, shell=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=0, start_new_session=(os.name == "posix"))
    output: list[str] = []
    saved_chars = 0
    truncated = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def consume(chunk: bytes, *, final: bool = False) -> None:
        nonlocal saved_chars, truncated
        value = decoder.decode(chunk, final=final)
        if not value:
            return
        if on_output:
            on_output(value)
        remaining = _MAX_OUTPUT - saved_chars
        if remaining > 0:
            output.append(value[:remaining])
            saved_chars += min(len(value), remaining)
        if len(value) > remaining:
            truncated = True

    timed_out = False
    selector: selectors.BaseSelector | None = None
    try:
        if os.name == "posix":
            selector = selectors.DefaultSelector()
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready = selector.select(min(remaining, 1.0))
                if not ready and on_checkpoint:
                    on_checkpoint()
                for key, _ in ready:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if chunk:
                        consume(chunk)
                    else:
                        selector.unregister(key.fileobj)
        else:
            try:
                raw, _ = process.communicate(timeout=timeout)
                consume(raw or b"")
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            raw, _ = _stop_process(process)
            if raw:
                consume(raw)
        else:
            process.wait()
    except KeyboardInterrupt:
        raw, _ = _stop_process(process)
        if raw:
            consume(raw)
        consume(b"", final=True)
        raise
    except Exception:
        _stop_process(process)
        raise
    finally:
        if selector is not None:
            selector.close()
    consume(b"", final=True)
    captured = "".join(output)
    if truncated:
        captured += "\n...[output truncated]"
    if timed_out:
        return _clip(f"Command timed out after {timeout} seconds.\n{captured}")
    return _clip(f"Exit code: {process.returncode}\n{captured}" if captured else f"Exit code: {process.returncode}")


def _resolve(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Path must stay inside the task worktree") from exc
    return target


def _clip(value: str) -> str:
    return value if len(value) <= _MAX_OUTPUT else value[:_MAX_OUTPUT] + "\n...[output truncated]"


def _list_files(root: Path, path: str = ".") -> str:
    start = _resolve(root, path)
    if not start.exists():
        raise FileNotFoundError(path)
    if start.is_file():
        return str(start.relative_to(root))
    results = []
    for current, dirs, files in os.walk(start):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED)
        for name in sorted(files):
            if name in _IGNORED:
                continue
            candidate = Path(current) / name
            results.append(str(candidate.relative_to(root)))
            if len(results) >= 3000:
                return "\n".join(results) + "\n...[file list capped at 3000]"
    return "\n".join(results) or "No files found."


def _search_files(root: Path, query: str, path: str | None = None) -> str:
    start = _resolve(root, path or ".")
    if not start.exists():
        raise FileNotFoundError(path)
    args = ["rg", "-n", "--hidden", "--glob", "!.git", "--glob", "!.git/**", "--glob", "!node_modules/**", "--glob", "!.venv/**", "--glob", "!venv/**", "--", query, str(start)]
    try:
        result = subprocess.run(args, cwd=root, text=True, capture_output=True, timeout=30)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "Search failed")
        return _clip(result.stdout.strip() or "No matches found.")
    except FileNotFoundError:
        pattern = re.compile(query)
        matches = []
        for current, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if d not in _IGNORED]
            for name in files:
                if name in _IGNORED:
                    continue
                file = Path(current) / name
                try:
                    for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                        if pattern.search(line):
                            matches.append(f"{file.relative_to(root)}:{number}:{line}")
                            if len(matches) >= 500:
                                return _clip("\n".join(matches) + "\n...[search capped at 500 matches]")
                except (OSError, UnicodeError):
                    continue
        return _clip("\n".join(matches) or "No matches found.")


def execute_tool(name: str, arguments: dict[str, Any], root: Path, *, auto_approve: bool = False,
                 on_output: Any | None = None, on_checkpoint: Any | None = None) -> str:
    if name == "list_files":
        return _list_files(root, str(arguments.get("path") or "."))
    if name == "read_file":
        path = _resolve(root, str(arguments["path"]))
        return _clip(path.read_text(encoding="utf-8"))
    if name == "write_file":
        path = _resolve(root, str(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(arguments["content"]), encoding="utf-8")
        return f"Wrote {path.relative_to(root)} ({path.stat().st_size} bytes)."
    if name == "apply_patch":
        patch = str(arguments["patch"])
        if not patch.strip():
            raise ValueError("Patch cannot be empty")
        check = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", "-"],
                              cwd=root, input=patch, text=True, capture_output=True)
        if check.returncode:
            raise RuntimeError(check.stderr.strip() or "Patch did not apply cleanly")
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                                 input=patch, text=True, capture_output=True)
        if applied.returncode:
            raise RuntimeError(applied.stderr.strip() or "Patch application failed")
        return "Patch applied successfully."
    if name == "search_files":
        return _search_files(root, str(arguments["query"]), arguments.get("path"))
    if name == "git_status":
        result = subprocess.run(["git", "status", "--short", "--branch"], cwd=root,
                                text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not read Git status")
        return _clip(result.stdout.strip() or "Working tree clean.")
    if name == "git_diff":
        result = subprocess.run(["git", "diff", "--no-ext-diff"], cwd=root,
                                text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not read Git diff")
        return _clip(result.stdout.strip() or "No tracked diff.")
    if name == "run_command":
        command = str(arguments["command"])
        timeout = max(1, min(600, int(arguments.get("timeout_seconds", 120))))
        if not auto_approve:
            try:
                answer = input(f"\nAllow command in task worktree?\n  {command}\n[y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer in {"/exit", "/quit"}:
                raise KeyboardInterrupt
            if answer not in {"y", "yes"}:
                return "Command denied by user. Continue using other tools or ask for guidance."
        return _run_command(command, root, timeout, on_output, on_checkpoint)
    raise ValueError(f"Unknown tool: {name}")
