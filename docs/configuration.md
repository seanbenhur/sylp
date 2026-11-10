# Configuration

Everrun keeps all of its state under one directory and is otherwise configured through environment variables and command-line flags.

## State layout

| Path | Contents |
| --- | --- |
| `~/.everrun/state.sqlite3` | Task records, event log, conversation messages, compaction checkpoints. |
| `~/.everrun/auth.json` | Saved provider credentials and remembered provider/model defaults. |
| `~/.everrun/tasks/<task-id>/` | Final review reports. |
| `~/.everrun/mcp.json` | MCP server configuration (also `<repo>/.everrun/mcp.json`). |
| `~/.everrun/plugins/` | Local plugin files (also `<repo>/.everrun/plugins/`). |
| `<repo>/.git/everrun-worktrees/<task-id>` | Per-task branches and worktrees, in the repository's common Git directory. |

The state directory and credential file are created with owner-only permissions (`0700` / `0600`). Set `EVERUN_HOME` to move the database, credentials, and reports elsewhere. An older `~/.relay` state directory is still read if `~/.everrun` has no database, so a rename does not strand task history.

## Environment variables

| Variable | Effect |
| --- | --- |
| `EVERUN_HOME` | Move Everrun's state directory. |
| `EVERUN_PROVIDER` | Default provider for `run`, `models`, and interactive sessions. |
| `EVERUN_MODEL` | Default model for `everrun run`. |
| `EVERUN_AUTO_APPROVE` | `1` lets interactive sessions run shell commands without per-command approval. Use only in a trusted, disposable environment. |
| `EVERUN_REQUEST_TIMEOUT` | Provider request timeout in seconds, 60-3600 (default 900). |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `ANTIGRAVITY_API_KEY`, `OPENROUTER_API_KEY` | Provider keys without saving them to `auth.json`. |
| `EVERUN_API_KEY`, `EVERUN_BASE_URL` | Key and endpoint for the `openai-compatible` provider. |
| `EVERUN_SANDBOX` | Shell sandbox mode: `off`, `auto` (default), or `require`. See [Extensions](extensions.md). |
| `EVERUN_SANDBOX_NETWORK` | `off` cuts network access inside sandboxed commands (default `on`). |
| `EVERUN_MCP_CONFIG` | Read MCP server configuration from this file instead of the default locations. |

## Safety

- Worktrees isolate edits; the shell sandbox (when available) also confines approved commands to the worktree with a read-only filesystem elsewhere. It is best-effort confinement, not a hard security boundary; see [Extensions](extensions.md) for the exact limits. Commands require approval unless auto-approve is on.
- Plugins run in-process with Everrun's full privileges and are not sandboxed. Install only plugins you trust.
- Auto-approve (`--auto-approve` or `EVERUN_AUTO_APPROVE=1`) removes the per-command check entirely; combine it with the sandbox and a disposable environment.
- Everrun records model-reported checks in the task report but does not independently validate them. Review the diff before merging.

## Development

Run the regression suite from a checkout (Python 3.11+):

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -v
```

The `benchmarks/` directory contains the Everrun vs. Codex comparison protocol; see [benchmarks/PROTOCOL.md](../benchmarks/PROTOCOL.md).
