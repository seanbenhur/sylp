# Architecture

Sylp is a standalone, local-first coding harness. It talks to model providers directly (no Codex CLI or Claude Code subprocess), runs its own model-and-tools loop inside an isolated Git worktree, and records everything it needs to resume in a local SQLite database.

## Package layout

| Module | Responsibility |
| --- | --- |
| `sylp_harness/cli.py` | Argument parsing, interactive startup flow, provider/model selection, session commands. |
| `sylp_harness/runner.py` | The task loop: sends history to the model, dispatches tool calls, checkpoints output, handles interrupts, waiting-for-input, and completion. |
| `sylp_harness/providers.py` | Provider adapters: credentials, model listing, and one `complete()` entry point per provider family (OpenAI Responses API, Anthropic Messages API, OpenAI-compatible chat completions, Antigravity Code Assist). |
| `sylp_harness/auth.py` | Browser OAuth sign-in for ChatGPT and Google Antigravity, token refresh, and reuse of an existing Antigravity IDE session when present. |
| `sylp_harness/tools.py` | The tool surface the model can call: file reads/writes, unified-diff patches, search, git status/diff, and shell commands with approval. |
| `sylp_harness/sandbox.py` | Best-effort confinement of approved shell commands via bubblewrap (Linux) or sandbox-exec (macOS), with a one-time backend probe and honest degradation. |
| `sylp_harness/mcp.py` | Stdlib-only MCP client: stdio JSON-RPC handshake, tool listing, and tool calls to configured MCP servers. |
| `sylp_harness/plugins.py` | Discovery, validation, and dispatch for local Python plugins that add model tools. |
| `sylp_harness/extensions.py` | Combines MCP servers and plugins into one tool registry per task run. |
| `sylp_harness/store.py` | SQLite persistence for tasks, events, conversation messages, and durable memories. WAL mode, owner-only file permissions. |
| `sylp_harness/memory.py` | Cross-session memory: extraction of durable facts at compaction and completion (session provider, or an optional local OpenAI-compatible extractor with provider fallback), and hybrid recall injected into the system prompt - FTS5 (LIKE fallback) fused via reciprocal rank fusion with local embeddings (sqlite-vec index when installed, Python scan otherwise). |
| `sylp_harness/git.py` | Worktree and branch preparation for each task. |
| `sylp_harness/terminal.py` | The full-screen interactive UI: chat transcript with `❯`/`•` message markers and inline markdown (`**bold**`, `` `code` ``, headings, fenced code blocks), a framed composer (separators, `❯` prompt, key hints, dim status row), an animated busy spinner with elapsed timer in the header (ticker thread serialized by a draw lock), a session rail (attention badges for sessions waiting for input), a toggleable changes pane with the task's diff summary, and tool-call output collapsed behind an F4 toggle with a hidden-line count placeholder. Rendering is span-based with a wrapped-row cache; panels read the task store and worktree through injectable providers; no pty layer. |

## Task lifecycle

1. **Create.** `sylp` or `sylp run` records the goal, acceptance criteria, provider, and model, then prepares a dedicated branch and worktree under `.git/sylp-worktrees/<task-id>` in the repository's common Git directory. The worktree starts from your current checkout, including tracked edits and untracked files, so the task sees the code you have open.
2. **Run.** The runner sends the system prompt, saved conversation, and tool schemas to the selected model. Tool calls are executed against the worktree and their results appended to the conversation. The loop continues until the model reports completion, asks for input, or is interrupted.
3. **Pause and resume.** `/exit`, Ctrl-C, or a hard kill pauses the task. Because messages, events, and tool-output checkpoints live in SQLite, `sylp resume <task-id>` continues from the saved conversation without depending on any external agent session.
4. **Review.** `/done` or completion writes a report to `~/.sylp/tasks/<task-id>/` with the goal, criteria, agent summary, diff summary, and provider-reported token usage. Sylp never merges for you: inspect the diff, then merge or discard the branch yourself.

## The model loop

Each turn, the runner:

- sends the full saved history (compacted when it grows past the history limit) plus tool schemas to `providers.complete()`;
- records the assistant message and any tool calls;
- executes each tool call in order, streaming shell output live and checkpointing it to the event log;
- continues until a turn produces no tool calls.

Empty model responses (no text and no tool calls) are captured with provider diagnostics, retried once, and then pause the session instead of looping silently. A turn that ends with `finish_reason` of `length`/`max_tokens` gets an automatic "continue" nudge.

## Memory

Compaction and task completion each trigger a best-effort extraction pass that distills durable facts into the `memories` table (scoped to the repository or the user, deduplicated on normalized exact match). At task start and resume, the goal plus the latest user message query the table - FTS5 keyword search, optionally fused with vector similarity from a local embeddings endpoint - and the top matches join the system prompt labeled as model-extracted hints to verify before relying on. Every failure mode (FTS5 missing, embedding or extractor endpoint down, malformed extraction JSON) degrades to the next simpler path and is recorded as an event rather than blocking the task.

## Interruption recovery

Shell tool output is checkpointed to the event log as it streams (on a size and time cadence). If the process dies mid-tool, resume finds the recorded result or the captured partial output and tells the model to inspect the worktree before considering the action again, which prevents duplicate side effects. Each session also holds a file lock so two Sylp processes cannot drive the same task at once.

## Tool surface

`list_files`, `read_file`, `write_file`, `apply_patch`, `search_files`, `git_status`, `git_diff`, and `run_command`. `run_command` requires interactive approval unless the task was started with `--auto-approve` (or `SYLP_AUTO_APPROVE=1` for interactive sessions). Approvals are per command; there is no persistent allowlist.

At task start the runner also loads configured MCP servers and local plugins and appends their schemas to the tool list; extension tools are dispatched through the same result-checkpointing path as built-ins. MCP servers are subprocesses terminated when the task pauses or finishes; plugins run in-process. A failed MCP server or plugin is skipped and recorded in an `extensions_loaded` event rather than stopping the task. See [Extensions](extensions.md).

## Isolation model

Worktrees keep task edits out of your checkout. On top of that, approved shell commands can run sandboxed (bubblewrap or sandbox-exec): writes are limited to the worktree, the repository's Git directory, and `/tmp`, everything else is read-only, and network access can be disabled. This is best-effort confinement rather than a hard security boundary, and plugins are not confined by it. Review commands before approving them, use repositories you trust, and read the report and diff before merging.
