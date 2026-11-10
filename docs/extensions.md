# Extensions: sandbox, MCP servers, and plugins

Sylp's built-in tools cover the repository basics. Three extension points add confinement and new capabilities: a shell sandbox, MCP (Model Context Protocol) servers, and local Python plugins.

## Shell sandbox

`run_command` can confine each approved shell command so it can write only to the task worktree, the repository's Git directory, and `/tmp`; the rest of the filesystem is read-only. Network access stays on by default and can be cut off.

| Setting | Values | Default |
| --- | --- | --- |
| `SYLP_SANDBOX` | `off`, `auto`, `require` | `auto` |
| `SYLP_SANDBOX_NETWORK` | `on`/`1` or `off`/`0`/`false`/`no` | `on` |

Backends, tried in order:

- **Linux:** [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`). Commands run in user/IPC/PID/UTS namespaces with the filesystem bound read-only except the writable paths above. `--die-with-parent` ensures no orphaned processes when a task is interrupted.
- **macOS:** `sandbox-exec` with a profile that denies writes outside the worktree, the Git directory, and the temp directories.

`auto` sandboxes when a backend exists and warns in the tool result when none is available (or when the backend fails a one-time probe, e.g. a kernel that forbids user namespaces). `require` refuses to run commands without a working backend. `off` restores the pre-sandbox behavior. The approval prompt shows the active backend, and `sylp sandbox` (with `--test` for a probe run) reports the current status.

The sandbox is best-effort confinement for honest mistakes and over-eager commands, not a hard security boundary against a determined adversary: it shares `/tmp`, allows network by default, and (on Linux) depends on unprivileged user namespaces. Keep reviewing commands before approving them, and combine the sandbox with a disposable environment for untrusted repositories. Plugins (below) run in-process and are **not** confined by it.

## MCP servers

Sylp speaks MCP over stdio (newline-delimited JSON-RPC 2.0). Configure servers in `~/.sylp/mcp.json`, `<repo>/.sylp/mcp.json` (repository entries win on a name conflict), or a file named by `SYLP_MCP_CONFIG`:

```json
{
  "servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/you/data"]
    },
    "slow-job": {
      "command": "/opt/tools/server",
      "env": {"TOKEN": "..."},
      "timeout_seconds": 300
    }
  }
}
```

When a task starts, Sylp launches each configured server, performs the `initialize` handshake, and lists its tools. The model sees them as `mcp__<server>__<tool>` (sanitized to tool-name characters, capped at 64 characters) with the server's own descriptions and input schemas. Results are flattened to text: text content items are joined; other content types are returned as JSON. A tool that reports `isError` surfaces as a tool error the model can react to.

A server that fails to start or handshake is skipped with a recorded `extensions_loaded` event and a printed notice; the task continues without its tools. Servers are terminated when the task pauses or finishes and relaunched on resume. `sylp mcp` lists configured servers and probes their tool lists without starting a task.

Known limits: stdio transport only (no HTTP/SSE), tools only (no resources or prompts), and no OAuth flows; put any needed credentials in the server's `env` block.

## Plugins

A plugin is a Python file in `~/.sylp/plugins/` or `<repo>/.sylp/plugins/` that defines a `register()` function returning tool dicts:

```python
# ~/.sylp/plugins/word_count.py
def _count(arguments, context):
    path = context["root"] / arguments["path"]
    return f"{len(path.read_text().split())} words"

def register():
    return [{
        "name": "word_count",
        "description": "Count words in a worktree file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
        "handler": _count,
    }]
```

Each tool dict needs a `name` (matching `^[a-zA-Z0-9_-]{1,64}$`, not colliding with a built-in tool or the reserved `mcp__` prefix), a `description`, an optional JSON-schema `parameters` object, and a callable `handler(arguments, context)`. `context["root"]` is the task worktree path. The handler's return value becomes the tool result; return a string.

Files starting with `_` are skipped. A plugin that fails to import or register is reported and skipped; it does not stop other plugins or the task. `sylp plugins` lists discovered plugins, their tools, and any load errors.

Plugins run in-process with Sylp's full privileges: they can read your files, use the network, and run commands without approval or sandboxing. Install only plugins you trust, and review them the way you would review a dependency.
