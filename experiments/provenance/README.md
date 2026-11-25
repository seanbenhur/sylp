# Sylp provenance: see how your code came to be

Local-first, open-source code provenance for AI coding agents. Click a code change and see the prompt, the assistant response and the tool action recorded around it. One small recorder works with Claude Code, Codex CLI and Cursor CLI, plus an evolution timeline that shows how the repository grew: prompts, tool windows, file changes, commits and branches in one view.

Inspired by delta.dev's "See how the code came to be". Delta is a hosted product by Zed Industries; this is not Delta, is not affiliated with it, implements only that one idea, and claims no Delta compatibility or parity. Other open-source projects explore the same space for a single host (for example promptrail and git-for-prompts); this spike's angle is three hosts from one recorder, evidence labels that keep unknowns unknown, and zero dependencies beyond Python's stdlib and SQLite. Nothing is uploaded anywhere.

## Status

Private experiment. Fixture-tested (31 checks, simulated hook payloads driving real file edits), **not yet verified in a live CLI session**. Hook event names and payload fields differ between hosts and may change between CLI versions. Use it only in a disposable repository.

## Install as a plugin (one command)

Needs Python 3.10+ and git. The plugin directories are committed under `plugins/`, so there is no build step.

```sh
git clone -b experiment/code-provenance-adapters https://github.com/seanbenhur/sylp.git ~/sylp-prov
cd ~/sylp-prov/experiments/provenance
```

**Cursor CLI** (`agent`):

```sh
python3 install.py --host cursor --repo ~/prov-test
cd ~/prov-test && agent
```

`install.py` creates the throwaway repo if missing, copies the plugin into `~/.cursor/plugins/local/sylp-provenance` (Cursor's documented local-plugin location, loaded by the editor and the CLI), and initializes the capture database. If your build blocks local plugin imports, launch with `agent --plugin-dir ~/sylp-prov/experiments/provenance/plugins/cursor` instead (`--plugin-dir` is documented in Cursor's CLI parameter reference; not exercised on a real install here).

**Codex CLI**:

```sh
python3 install.py --host codex --repo ~/prov-test2
cd ~/prov-test2 && codex
```

This writes a project-level `.codex/hooks.json` (a documented Codex hook location) pointing at the committed plugin by absolute path. Codex skips untrusted hooks: open `/hooks` in the CLI and trust the sylp provenance hooks first. `install.py --host codex --marketplace` instead installs through the documented personal-marketplace flow (`~/.codex/plugins/` + `~/.agents/plugins/marketplace.json`, then `/plugins` to enable); that flow is written from the official docs and is not verified against a live Codex install.

**Claude Code**:

```sh
python3 install.py --host claude --repo ~/prov-test3
cd ~/prov-test3 && claude --plugin-dir ~/sylp-prov/experiments/provenance/plugins/claude
```

Then give the agent one small prompt that edits `a.py`, and render:

```sh
python3 ~/sylp-prov/experiments/provenance/recorder.py render ~/history.html
```

Open `~/history.html`. The render command is host-independent; it reads only the local database and the repository's git history.

Trial caveats:

- The first prompt you give uses your own plan/quota with that host. Whether to spend it is your call. Check `agent --version` / `codex --version` / `claude --version` and your plan first.
- Captured text stays local and unencrypted, which is why the repo must be a throwaway.
- The default database supports ONE repository. Use `--db` (or `SYLP_PROVENANCE_DB`) per extra repo, outside the repo.
- Uninstall: remove the plugin folder (`~/.cursor/plugins/local/sylp-provenance`, or `~/.codex/plugins/sylp-provenance`), any `.codex/hooks.json` written by the installer, and the database.

## What you get

`history.html` opens with **How this repository evolved**: a graph with prompts, tool windows and recorded changes on one lane and git commits on the other, edges connecting each change to the prompt and tool window behind it. Click a prompt or change node to jump to its thread below. Commits are labeled by their relationship to recorded evidence: inside a recorded tool window (possibly agent-made, not proven), during a recorded session, or outside recorded sessions. If the hooks did not fire in a live session, the page says so plainly and shows only the commits.

Below the graph, **The conversations behind the changes**: each recorded prompt as a thread - your prompt, the recorded assistant response, and the diffs produced around it, each with an evidence label (see below). Changes without a recorded prompt are grouped separately and never guessed at. Nothing is uploaded; the page makes no network calls.

## How it works: hooks, not skills

Capture uses each host's **hooks**: lifecycle events (`sessionStart`/`beforeSubmitPrompt`/`preToolUse`/`postToolUse`/`stop` on Cursor; `SessionStart`/`UserPromptSubmit`/`PreToolUse`/`PostToolUse`/`Stop`/`SessionEnd` on Codex and Claude) delivered as JSON payloads to the recorder. Hooks fire deterministically on every event, which is what makes provenance capture reliable.

Agent **skills** (SKILL.md guidance shipped in a plugin) can steer an agent's behavior, but they load at the model's discretion, carry no event payloads, and cannot guarantee that anything is recorded per edit. A skill may later complement the plugin (for example a command that renders the report from inside a session), but skills alone cannot capture provenance. All three hosts support plugin-bundled hooks, so capture stays on hooks.

Each host gets a self-contained directory under `plugins/<host>/` with its manifest (`.cursor-plugin/plugin.json`, `.codex-plugin/plugin.json` plus a portable root `plugin.json`, `.claude-plugin/plugin.json`), `hooks/hooks.json`, and copies of the shared `recorder.py` and `adapters.py`. Tests enforce that the copies never drift from the shared code.

## Host adapters

- Claude: prompt_id, session_id, tool_use_id; Claude lifecycle names retained.
- Codex: turn_id maps to the canonical prompt ID. apply_patch header paths support multiple file associations. Nonzero shell exits still use PostToolUse. No-op hook output is `{}`, including Stop. Paths declared in a patch establish association, not proof that the patch applied.
- Cursor: conversation_id/generation_id map to canonical session/turn IDs. Native camelCase events map to common recorder events. afterAgentResponse text is kept separately and linked to the corresponding turn. Missing cwd is accepted only with exactly one workspace root. Multi-root ambiguity fails visibly rather than choosing one.

Host/session namespacing prevents collisions. Missing tool IDs are rejected rather than paired by time. The generic tool pre/post path is used for Cursor, not a simultaneous afterFileEdit subscription that could double-count a change. Unknown tool names are association-only, not silently treated as file edits. Codex delayed command completion retains its pending tool window.

## Repeatable checks

```sh
python3 -m unittest discover -s tests -v
python3 demo.py /tmp/sylp-claude-fixture --host claude
python3 demo.py /tmp/sylp-codex-fixture --host codex
python3 demo.py /tmp/sylp-cursor-fixture --host cursor
```

Each demo produces its own history.html and synthetic-hooks.json. These are real file edits through simulated events, not real agent chats. Generate examples locally; captured databases and HTML exports are not committed. The tests include native payload normalization, two-file patches, failed shell writes, delayed completion, session isolation, duplicate posts, plugin packaging integrity, the evolution timeline, and exact hook command execution from the committed plugin directories.

`build_bundles.py` still generates the older `dist/` bundles and remains covered by tests, but the committed `plugins/` directories are the install path; no build step is needed.

## Remaining validation gates

All host integrations remain unverified until a version-pinned live session demonstrates actual prompt IDs, file-change hooks, assistant response association, failure events and restart behavior. Host schema differences and policy settings may require adjustments. No supported-version floor is claimed. This is not a production-compatible support matrix.

## Shared recorder details

Experimental code, based on sylp main `ca79119`. No changes to the existing harness coding loop. Not released and not validated in any installed host CLI session.

## What works in the included fixture

A stdlib Python recorder stores hook boundaries and content-addressed text-file snapshots in SQLite. Each historical diff links to its prompt ID, recorded prompt and final assistant response when supplied. A self-contained HTML viewer supports filtering by filename, expanding boundary evidence, and the repository evolution timeline. It makes no network or model calls and creates no repository commits.

The demo modifies real files, sending synthetic Claude Code-style payloads through a fresh recorder process for each event. It is not a recorded Claude conversation. The core tests cover duplicate tool events, overlap, manual changes, failed-tool writes, restart state, edit/revert, exclusions, oversized files, rename handling, HTML escaping, pause and repository isolation.

## Try the deterministic fixture

Python 3.10+ and Git, no dependencies:

```sh
python3 -m unittest discover -s tests -v
python3 demo.py /tmp/sylp-provenance-example
```

Open `/tmp/sylp-provenance-example/history.html`. The output contains local fixture code and prompts. It is private unless you share it.

## Pause and deletion

Run the selected plugin's recorder with `pause` to stop new capture or `enable` to resume. This does not erase history. To delete history, close all recorder processes and remove the database and its `-wal` / `-shm` siblings. Delete HTML exports separately. Earlier snapshots remain after a source file is removed. No secure-erasure guarantee.

## Evidence labels

- **Tool-linked:** an Edit/Write tool named this file, and the file changed between recorded before/after boundaries. This is evidence of association, not proof that no human or other process touched it.
- **Associated:** another tool window, such as Bash, contained this change. No claimed causal attribution.
- **Ambiguous:** overlapping tool windows or an interrupted/pending operation prevent safe attribution.
- **Unattributed:** a change was observed outside a matching tool window.
- **Capture gap:** a file is no longer eligible for capture, not a verified deletion.

Rename currently appears as deletion plus addition. The viewer shows historical file diffs, not present-day line blame. Prompt-to-edit evidence is based on IDs provided by the host; missing IDs mean missing associations, not guessed ones. Commit labels in the evolution timeline describe timing relative to recorded windows, never proven authorship.

## Privacy and missing coverage

Repository opt-in authorizes local capture of submitted prompts and eligible code. Prompt text and code can contain secrets. Filename exclusions are NOT a complete secret scanner. Do not use this on sensitive repositories yet. Captured text is stored unencrypted, with restrictive file permissions. There is no remote service, telemetry or upload.

Git-ignored untracked files are excluded, plus selected credential names/directories/extensions, symlinks, binary/non-UTF8 files, files over 128KiB and snapshots over 8MiB or 2000 candidate files. Already tracked files remain candidates even if later ignored, except explicit exclusions. Capture errors are printed to stderr and do not block the host agent. There is no durable error dashboard or reconciliation after an unobserved failure yet.

Only hook-supplied submitted prompts, final assistant text and selected metadata are captured. Full transcript import, intermediate assistant messages, full tool input/output, tests-as-evidence, transcript reconciliation, automatic file watching, subagent identity display, retention/quotas, search across prompts, current-line mapping, sharing/sync and restore are NOT implemented. Edits and reversions wholly inside one tool window can be missed. Background work, concurrent sessions and manual changes can be only temporally associated. The plugin observes work; it does not execute, resume or control the host agent.

## Inspection sources

Reviewed 23 November 2026:

- https://github.com/seanbenhur/sylp
- https://delta.dev/deltadb
- https://delta.dev/docs/concepts/delta-and-git
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/plugins/components

Plugin packaging, hooks and marketplace references reviewed 24 November 2026:

- https://cursor.com/docs/plugins
- https://cursor.com/docs/reference/plugins
- https://cursor.com/docs/hooks
- https://cursor.com/docs/cli/reference/parameters
- https://learn.chatgpt.com/docs/hooks
- https://learn.chatgpt.com/docs/plugins
- https://developers.openai.com/plugins/build/plugins

Adjacent open-source projects seen 24 November 2026 (single-host each): https://github.com/thisalihassan/promptrail, https://github.com/gauravkrp/git-for-prompts.

This independently written experiment implements a small provenance slice. It does not copy Delta's UI/assets, use DeltaDB, or claim Delta compatibility/parity.
