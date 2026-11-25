# sylp-provenance for Codex

Local-first code provenance: records each prompt and tool-call boundary on your machine and links it to the file changes it caused, so you can ask "which prompt wrote this line?" and draft PR evidence from real history. Nothing is uploaded.

## What it does

- Lifecycle hooks capture prompts and tool-call boundaries into a local SQLite database per repo under `~/.sylp-provenance/repos/`, created automatically on the first hook event.
- A stdio MCP server (declared in `.mcp.json`) adds two tools: `why` (trace a file, line or symbol back to the prompt that wrote it) and `pr_summary` (assemble the prompts and diffs behind a branch). If Codex does not expand the plugin-relative path, `install.py` prints an absolute-path `codex mcp add` fallback.
- Skills: `serve-dashboard` starts a read-only dashboard on localhost, `demo` builds a synthetic showcase session, and `pr-evidence` drafts a per-file provenance summary. It never opens a PR by itself.

## Install

```
python3 experiments/provenance/install.py --host codex --marketplace
```

Then open `codex`, run `/plugins` to enable sylp-provenance, and `/hooks` to trust its hooks (Codex skips untrusted hooks).

## Status

Experimental. 39 automated tests pass against fixtures; the hooks and MCP wiring are written from OpenAI's docs and have not yet been verified in a live Codex install. While installed, the plugin records prompts and code from every repo you open; captured text stays local and unencrypted, so prefer throwaway repos until you trust it. `recorder.py pause` pauses capture per repo, and uninstalling removes capture entirely.

License: Apache-2.0 (see the repository root).
