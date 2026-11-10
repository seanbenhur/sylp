# Configuration

Sylp keeps all of its state under one directory and is otherwise configured through environment variables and command-line flags.

## State layout

| Path | Contents |
| --- | --- |
| `~/.sylp/state.sqlite3` | Task records, event log, conversation messages, compaction checkpoints, durable memories. |
| `~/.sylp/auth.json` | Saved provider credentials and remembered provider/model defaults. |
| `~/.sylp/tasks/<task-id>/` | Final review reports. |
| `~/.sylp/mcp.json` | MCP server configuration (also `<repo>/.sylp/mcp.json`). |
| `~/.sylp/plugins/` | Local plugin files (also `<repo>/.sylp/plugins/`). |
| `<repo>/.git/sylp-worktrees/<task-id>` | Per-task branches and worktrees, in the repository's common Git directory. |

The state directory and credential file are created with owner-only permissions (`0700` / `0600`). Set `SYLP_HOME` to move the database, credentials, and reports elsewhere. State directories from earlier names (`~/.everrun`, then `~/.relay`) are still read when `~/.sylp` has no database, and existing credentials in `~/.everrun` keep working, so a rename does not strand task history.

## Environment variables

| Variable | Effect |
| --- | --- |
| `SYLP_HOME` | Move Sylp's state directory. |
| `SYLP_PROVIDER` | Default provider for `run`, `models`, and interactive sessions. |
| `SYLP_MODEL` | Default model for `sylp run`. |
| `SYLP_AUTO_APPROVE` | `1` lets interactive sessions run shell commands without per-command approval. Use only in a trusted, disposable environment. |
| `SYLP_REQUEST_TIMEOUT` | Provider request timeout in seconds, 60-3600 (default 900). |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `ANTIGRAVITY_API_KEY`, `OPENROUTER_API_KEY` | Provider keys without saving them to `auth.json`. |
| `SYLP_API_KEY`, `SYLP_BASE_URL` | Key and endpoint for the `openai-compatible` provider. |
| `SYLP_SANDBOX` | Shell sandbox mode: `off`, `auto` (default), or `require`. See [Extensions](extensions.md). |
| `SYLP_SANDBOX_NETWORK` | `off` cuts network access inside sandboxed commands (default `on`). |
| `SYLP_MCP_CONFIG` | Read MCP server configuration from this file instead of the default locations. |
| `SYLP_MEMORY` | `0` disables cross-session memory extraction and recall (default on). |
| `SYLP_EMBED_URL`, `SYLP_EMBED_MODEL`, `SYLP_EMBED_KEY` | Optional local OpenAI-compatible embeddings endpoint for hybrid (keyword + vector) memory retrieval, e.g. Ollama. Unset or unreachable means FTS5 keyword search only. |
| `SYLP_MEMORY_EXTRACT_URL`, `SYLP_MEMORY_EXTRACT_MODEL`, `SYLP_MEMORY_EXTRACT_KEY` | Optional local OpenAI-compatible chat endpoint that runs the memory extraction pass instead of the session provider. Falls back to the session provider on failure. |

## Memory

Sylp distills durable facts (repository conventions, user preferences, decisions, failure causes) into the `memories` table at compaction and task completion, and injects relevant ones into later sessions' system prompts with a verify-before-use caveat. Search is SQLite FTS5 with a `LIKE` fallback; the optional embedding endpoint above adds vector similarity fused with keyword ranking (sqlite-vec index when installed, Python scan otherwise). Inspect with `sylp memory`, remove entries with `sylp memory --forget <id>`, or disable the feature with `SYLP_MEMORY=0`.

## Safety

- Worktrees isolate edits; the shell sandbox (when available) also confines approved commands to the worktree with a read-only filesystem elsewhere. It is best-effort confinement, not a hard security boundary; see [Extensions](extensions.md) for the exact limits. Commands require approval unless auto-approve is on.
- Plugins run in-process with Sylp's full privileges and are not sandboxed. Install only plugins you trust.
- Auto-approve (`--auto-approve` or `SYLP_AUTO_APPROVE=1`) removes the per-command check entirely; combine it with the sandbox and a disposable environment.
- Sylp records model-reported checks in the task report but does not independently validate them. Review the diff before merging.

## Development

Run the regression suite from a checkout (Python 3.11+):

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -v
```

The `benchmarks/` directory contains the Sylp vs. Codex comparison protocol; see [benchmarks/PROTOCOL.md](../benchmarks/PROTOCOL.md).
