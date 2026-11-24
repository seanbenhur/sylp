# Sylp provenance / three-host private spike

Version 0.0.2 adds separate Claude Code, Codex CLI and Cursor CLI capture adapters around one local recorder/viewer. None has been loaded in a real host CLI here. No authenticated model session or paid use was attempted. Generated hook commands were executed independently using fixture payloads, not by the host programs.

## Host adapters

Run `python3 build_bundles.py` to prepare self-contained folders in `dist/claude`, `dist/codex` and `dist/cursor`. Each contains only its own manifest/hooks, recorder and shared adapter module. Do not mix the hooks files between hosts or load all bundles in one host.

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

Each demo produces its own history.html and synthetic-hooks.json. These are real file edits through simulated events, not real agent chats. Generate examples locally; captured databases and HTML exports are not committed. The tests include native payload normalization, two-file patches, failed shell writes, delayed completion, session isolation, duplicate posts and exact hook command execution from generated bundles.

## Local integration trial, not performed here

Use only a disposable repository, never sensitive or employer code. Review the capture policy below first. Initialize explicitly using the selected bundle's recorder:

```sh
python3 /absolute/path/to/dist/HOST/recorder.py init /absolute/path/to/disposable-repo --accept-local-capture
```

The default database supports ONE repository. Set `SYLP_PROVENANCE_DB` to a separate absolute path OUTSIDE that repository for each test; keep it set for both initialization and the CLI process.

- Claude trial: `claude --plugin-dir /absolute/path/to/dist/claude`
- Cursor trial: `agent --plugin-dir /absolute/path/to/dist/cursor`
- Codex bundle: prepared with .codex-plugin/plugin.json and hooks/hooks.json. No Codex local-plugin loading command has been exercised here. Use the installed version's supported local plugin workflow and review/trust hooks in `/hooks`. As an alternative, a reviewed project .codex/hooks.json can call the bundle's recorder via an absolute path. Do not overwrite existing project hooks or bypass hook trust.

First check CLI version, available loading flags and account/billing status. No plan, credentials or paid usage is included or authorized by this package. A real integration test still needs the user-selected account/plan and confirmation of any quota or charge use.

Render from the same selected database with `python3 recorder.py render /tmp/history.html`. No plugin registration, marketplace action, account login, repository push or publication is performed by these build scripts.

## Step-by-step Mac trial (Cursor first)

Concrete commands for a first live check on a Mac with Python 3.10+ and git. This expands the "Local integration trial" section above; the same capture policy and warnings apply. Not yet exercised on a real Cursor CLI.

1. Get the code and build the bundles:

```sh
git clone -b experiment/code-provenance-adapters https://github.com/seanbenhur/sylp.git ~/sylp-prov
cd ~/sylp-prov/experiments/provenance
python3 --version   # needs 3.10+
python3 build_bundles.py
```

Optional sanity check: `python3 -m unittest discover -s tests`

2. Make a throwaway repo (never sensitive or employer code):

```sh
mkdir ~/prov-test && cd ~/prov-test
git init && echo "print('hi')" > a.py && git add . && git commit -m init
```

3. Opt in to local capture, in the same terminal you will use for the CLI:

```sh
export SYLP_PROVENANCE_DB=$HOME/prov-cursor.db
python3 ~/sylp-prov/experiments/provenance/dist/cursor/recorder.py init ~/prov-test --accept-local-capture
```

The default database supports ONE repository; keep `SYLP_PROVENANCE_DB` set for both init and the CLI process, and use a separate path per test.

4. Check `agent --version` and your plan/billing status before any prompt, then launch:

```sh
cd ~/prov-test && agent --plugin-dir ~/sylp-prov/experiments/provenance/dist/cursor
```

5. Give it one small prompt that edits `a.py`, then render:

```sh
python3 ~/sylp-prov/experiments/provenance/dist/cursor/recorder.py render ~/history.html
```

Open `~/history.html` in a browser.

Caveats for this trial:

- `agent --plugin-dir` comes from Cursor's CLI docs and has not been run here. If your version rejects it, keep the exact error and `agent --version` output.
- The step 5 prompt uses your own Cursor quota/billing; whether to spend it is your call.
- Captured text stays local and unencrypted, which is why the repo must be a throwaway.
- Codex needs different setup: no verified local-plugin load command exists yet, so follow the Codex notes under "Local integration trial" until a verified flow replaces them.

## Remaining validation gates

All host integrations remain unverified until a version-pinned live session demonstrates actual prompt IDs, file-change hooks, assistant response association, failure events and restart behavior. Host schema differences and policy settings may require adjustments. No supported-version floor is claimed. This is not a production-compatible support matrix.

## Shared recorder details

Experimental code, based on sylp main `ca79119`. No changes to the existing harness coding loop. Not released and not validated in any installed host CLI session.

## What works in the included fixture

A stdlib Python recorder stores hook boundaries and content-addressed text-file snapshots in SQLite. Each historical diff links to its prompt ID, recorded prompt and final assistant response when supplied. A self-contained HTML viewer supports filtering by filename and expanding boundary evidence. It makes no network or model calls and creates no repository commits.

The demo modifies real files, sending synthetic Claude Code-style payloads through a fresh recorder process for each event. It is not a recorded Claude conversation. The core tests cover duplicate tool events, overlap, manual changes, failed-tool writes, restart state, edit/revert, exclusions, oversized files, rename handling, HTML escaping, pause and repository isolation.

## Try the deterministic fixture

Python 3.10+ and Git, no dependencies:

```sh
python3 -m unittest discover -s tests -v
python3 demo.py /tmp/sylp-provenance-example
```

Open `/tmp/sylp-provenance-example/history.html`. The output contains local fixture code and prompts. It is private unless you share it.

## Pause and deletion

Run the selected bundle's recorder with `pause` to stop new capture or `enable` to resume. This does not erase history. To delete history, close all recorder processes and remove the database and its `-wal` / `-shm` siblings. Delete HTML exports separately. Earlier snapshots remain after a source file is removed. No secure-erasure guarantee.

## Evidence labels

- **Tool-linked:** an Edit/Write tool named this file, and the file changed between recorded before/after boundaries. This is evidence of association, not proof that no human or other process touched it.
- **Associated:** another tool window, such as Bash, contained this change. No claimed causal attribution.
- **Ambiguous:** overlapping tool windows or an interrupted/pending operation prevent safe attribution.
- **Unattributed:** a change was observed outside a matching tool window.
- **Capture gap:** a file is no longer eligible for capture, not a verified deletion.

Rename currently appears as deletion plus addition. The viewer shows historical file diffs, not present-day line blame. Prompt-to-edit evidence is based on IDs provided by the host; missing IDs mean missing associations, not guessed ones.

## Privacy and missing coverage

Repository opt-in authorizes local capture of submitted prompts and eligible code. Prompt text and code can contain secrets. Filename exclusions are NOT a complete secret scanner. Do not use this on sensitive repositories yet. Captured text is stored unencrypted, with restrictive file permissions. There is no remote service, telemetry or upload.

Git-ignored untracked files are excluded, plus selected credential names/directories/extensions, symlinks, binary/non-UTF8 files, files over 128KiB and snapshots over 8MiB or 2000 candidate files. Already tracked files remain candidates even if later ignored, except explicit exclusions. Capture errors are printed to stderr and do not block Claude. There is no durable error dashboard or reconciliation after an unobserved failure yet.

Only hook-supplied submitted prompts, final assistant text and selected metadata are captured. Full transcript import, intermediate assistant messages, full tool input/output, tests-as-evidence, transcript reconciliation, automatic file watching, subagent identity display, retention/quotas, search across prompts, current-line mapping, sharing/sync and restore are NOT implemented. Edits and reversions wholly inside one tool window can be missed. Background work, concurrent sessions and manual changes can be only temporally associated. The plugin observes work; it does not execute, resume or control the host agent.

## Inspection sources

Reviewed 23 November 2026:

- https://github.com/seanbenhur/sylp
- https://delta.dev/deltadb
- https://delta.dev/docs/concepts/delta-and-git
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/plugins/components

This independently written experiment implements a small provenance slice. It does not copy Delta's UI/assets, use DeltaDB, or claim Delta compatibility/parity.

## Additional adapter references

Read 23 November 2026:
- https://learn.chatgpt.com/docs/hooks
- https://cursor.com/docs/hooks
- https://cursor.com/docs/cli/changelog
- https://cursor.com/docs/cli/reference/parameters
- https://cursor.com/docs/plugins/building

Use the separate dist/HOST bundle and host-specific guidance above for any trial.
