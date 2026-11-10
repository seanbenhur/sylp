# Architecture

Everrun is a standalone, local-first coding harness. It talks to model providers directly (no Codex CLI or Claude Code subprocess), runs its own model-and-tools loop inside an isolated Git worktree, and records everything it needs to resume in a local SQLite database.

## Package layout

| Module | Responsibility |
| --- | --- |
| `everrun_harness/cli.py` | Argument parsing, interactive startup flow, provider/model selection, session commands. |
| `everrun_harness/runner.py` | The task loop: sends history to the model, dispatches tool calls, checkpoints output, handles interrupts, waiting-for-input, and completion. |
| `everrun_harness/providers.py` | Provider adapters: credentials, model listing, and one `complete()` entry point per provider family (OpenAI Responses API, Anthropic Messages API, OpenAI-compatible chat completions, Antigravity Code Assist). |
| `everrun_harness/auth.py` | Browser OAuth sign-in for ChatGPT and Google Antigravity, token refresh, and reuse of an existing Antigravity IDE session when present. |
| `everrun_harness/tools.py` | The tool surface the model can call: file reads/writes, unified-diff patches, search, git status/diff, and shell commands with approval. |
| `everrun_harness/store.py` | SQLite persistence for tasks, events, and conversation messages. WAL mode, owner-only file permissions. |
| `everrun_harness/git.py` | Worktree and branch preparation for each task. |
| `everrun_harness/terminal.py` | The full-screen interactive UI (transcript, input, status line). |

## Task lifecycle

1. **Create.** `everrun` or `everrun run` records the goal, acceptance criteria, provider, and model, then prepares a dedicated branch and worktree under `.git/everrun-worktrees/<task-id>` in the repository's common Git directory. The worktree starts from your current checkout, including tracked edits and untracked files, so the task sees the code you have open.
2. **Run.** The runner sends the system prompt, saved conversation, and tool schemas to the selected model. Tool calls are executed against the worktree and their results appended to the conversation. The loop continues until the model reports completion, asks for input, or is interrupted.
3. **Pause and resume.** `/exit`, Ctrl-C, or a hard kill pauses the task. Because messages, events, and tool-output checkpoints live in SQLite, `everrun resume <task-id>` continues from the saved conversation without depending on any external agent session.
4. **Review.** `/done` or completion writes a report to `~/.everrun/tasks/<task-id>/` with the goal, criteria, agent summary, diff summary, and provider-reported token usage. Everrun never merges for you: inspect the diff, then merge or discard the branch yourself.

## The model loop

Each turn, the runner:

- sends the full saved history (compacted when it grows past the history limit) plus tool schemas to `providers.complete()`;
- records the assistant message and any tool calls;
- executes each tool call in order, streaming shell output live and checkpointing it to the event log;
- continues until a turn produces no tool calls.

Empty model responses (no text and no tool calls) are captured with provider diagnostics, retried once, and then pause the session instead of looping silently. A turn that ends with `finish_reason` of `length`/`max_tokens` gets an automatic "continue" nudge.

## Interruption recovery

Shell tool output is checkpointed to the event log as it streams (on a size and time cadence). If the process dies mid-tool, resume finds the recorded result or the captured partial output and tells the model to inspect the worktree before considering the action again, which prevents duplicate side effects. Each session also holds a file lock so two Everrun processes cannot drive the same task at once.

## Tool surface

`list_files`, `read_file`, `write_file`, `apply_patch`, `search_files`, `git_status`, `git_diff`, and `run_command`. `run_command` requires interactive approval unless the task was started with `--auto-approve` (or `EVERUN_AUTO_APPROVE=1` for interactive sessions). Approvals are per command; there is no persistent allowlist.

## Isolation model

Worktrees keep task edits out of your checkout, but Everrun is not a security sandbox: shell commands run as you, on your machine. Review commands before approving them, use repositories you trust, and read the report and diff before merging.
