# sylp-provenance for Cursor

Local-first code provenance: records each prompt and tool-call boundary on your machine and links it to the file changes it caused, so you can ask "which prompt wrote this line?" and draft PR evidence from real history. Nothing is uploaded.

## What it does

- Lifecycle hooks capture prompts and tool-call boundaries into a local SQLite database per repo under `~/.sylp-provenance/repos/`, created automatically on the first hook event.
- A stdio MCP server (declared in `mcp.json` with `${CURSOR_PLUGIN_ROOT}`) adds two tools: `why` (trace a file, line or symbol back to the prompt that wrote it) and `pr_summary` (assemble the prompts and diffs behind a branch).
- Commands: `/sylp-provenance:serve` starts a read-only dashboard on localhost, `/sylp-provenance:demo` builds a synthetic showcase session, and `/sylp-provenance:pr-evidence` drafts a per-file provenance summary. It never opens a PR by itself.

## Install

Local install: copy this folder to `~/.cursor/plugins/local/sylp-provenance`. Once listed on the Cursor Marketplace, search for "sylp-provenance" in Customize instead.

## Support and privacy

Tested against fixtures (54 automated tests). The hooks and MCP wiring follow Cursor's docs and have not been run in a live Cursor CLI session yet. While installed, the plugin records prompts and code from every repo you open; captured text stays local and unencrypted. `recorder.py pause` pauses capture per repo, and uninstalling removes capture entirely.

License: Apache-2.0 (see the repository root).
