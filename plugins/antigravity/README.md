# sylp-provenance for Antigravity

Local-first code provenance: records each prompt and tool-call boundary on your machine and links it to the file changes it caused, so you can ask "which prompt wrote this line?" and draft PR evidence from real history. Nothing is uploaded.

## What it does

- Lifecycle hooks (`hooks.json`) capture prompts and tool-call boundaries into a local SQLite database per repo under `~/.sylp-provenance/repos/`, created automatically on the first hook event.
- A stdio MCP server (`mcp_config.json`) adds two tools: `why` (trace a file, line or symbol back to the prompt that wrote it) and `pr_summary` (assemble the prompts and diffs behind a branch).
- Skills: `serve-dashboard` starts a read-only dashboard on localhost, `demo` builds a synthetic showcase session, and `pr-evidence` drafts a per-file provenance summary. It never opens a PR by itself.

## Install

`agy plugin install ./plugins/antigravity` from a clone of this repo, then restart Antigravity so the hooks load.

## How capture differs from other hosts

- Antigravity hook payloads carry no event name, so each hook command passes `--event`.
- There is no prompt-submit event. The prompt is read from `lastUserInput` at `PreInvocation` and recorded once per distinct text per conversation.
- Tool calls have no ID. A tool step is identified by `conversationId` plus `stepIdx`. The docs do not say Pre and Post share a `stepIdx`; the plugin assumes they do and this is not yet confirmed in a live session.
- Edits by `write_to_file`, `replace_file_content` and `multi_replace_file_content` are linked through their declared `TargetFile`. Edits made through `run_command` are associated by time window only.
- Hooks apply to the Antigravity CLI, IDE and App. One IDE report says hooks did not fire in the IDE; this is unconfirmed here.

## Support and privacy

Tested against fixtures, not yet run in a live Antigravity session. While installed, the plugin records prompts and code from every repo you open; captured text stays local and unencrypted. `recorder.py pause` pauses capture per repo, and uninstalling removes capture entirely.

License: Apache-2.0 (see the repository root).
