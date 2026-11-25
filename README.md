# Sylp provenance: see how your code came to be

Local-first code provenance for AI coding agents. Click a change and see the prompt and tool action behind it. One plugin works with Claude Code, Codex CLI and Cursor CLI. Everything stays on your machine; nothing is uploaded.

## Status

Public experiment. Fixture-tested (39 checks, simulated hook payloads driving real file edits), **not yet verified in a live CLI session**. Use it in a disposable repository. Captured text stays local and unencrypted.

## Install

**Claude Code** (native install, no clone):

```sh
claude plugin marketplace add seanbenhur/sylp
claude plugin install sylp-provenance@sylp-provenance
```

**Cursor CLI** (`agent`): Cursor has no install command for local plugins, so copy the plugin folder once:

```sh
git clone https://github.com/seanbenhur/sylp.git
cp -R sylp/plugins/cursor ~/.cursor/plugins/local/sylp-provenance
```

**Codex CLI**:

```sh
git clone https://github.com/seanbenhur/sylp.git
python3 sylp/install.py --host codex --marketplace
```

Then in `codex`, run `/plugins` to enable sylp-provenance and `/hooks` to trust its hooks.

No per-repo setup: once the plugin is enabled, its hooks record every repo you open in that host, each into its own database under `~/.sylp-provenance/repos/`.

## Use it, from inside the agent

Open any repo in your agent and work as usual; the hooks record automatically. Then:

- Ask **"why is this line here?"** - the agent answers through the plugin's MCP server from recorded evidence, with an honest "no recorded session" when there is nothing.
- **Dashboard**: `/sylp-provenance:serve` (Claude, Cursor) or the `serve-dashboard` skill (Codex) starts a read-only, localhost-only dashboard that updates live while your agent works.
- **Demo without spending quota**: `/sylp-provenance:demo` builds a synthetic throwaway session and renders it. A pre-rendered example is at [examples/history.html](examples/history.html).
- **PR evidence**: `/sylp-provenance:pr-evidence` drafts a per-file provenance summary; it never opens a PR by itself.

## How it works

Each host's lifecycle hooks send prompt and tool-call boundaries to a small stdlib-Python recorder, which stores them with content-addressed snapshots in SQLite. A change is linked to a prompt only on an exact content match; what cannot be matched is labeled, never guessed. Evidence labels: **tool-linked** (an Edit/Write tool named the file and it changed inside that window), **associated**, **ambiguous**, **unattributed**, **capture gap**.

## Limits

- Fixture-tested only. Hook payload fields differ between hosts and may change between CLI versions; no supported-version floor is claimed.
- Prompts and code can contain secrets. The filename exclusions are not a secret scanner, so do not use this on sensitive repositories yet.
- Captured text is stored unencrypted locally. `recorder.py pause` pauses a repo; to erase history, close the recorder and delete the database under `~/.sylp-provenance`. No secure-erasure guarantee.
- The plugin observes work; it does not execute, resume or control the host agent.

## Tests

Python 3.10+ and git, no dependencies:

```sh
python3 -m unittest discover -s tests -v
```

To verify end to end inside a real host, paste [TESTING-AGENT-PROMPT.md](TESTING-AGENT-PROMPT.md) into your agent; it installs into a throwaway repo, runs one small recorded prompt and reports pass or fail. It states the quota cost first and waits for your go-ahead.

License: Apache-2.0.
