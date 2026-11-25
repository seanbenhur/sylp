---
name: sylp-provenance-test
description: Install the sylp provenance plugin on Claude Code, Cursor CLI or Codex CLI and verify end to end that prompts, tool actions and file changes are captured and rendered into the graph view. Use when the user asks to set up, install, test, or verify the sylp provenance plugin.
---

# Set up and test the sylp provenance plugin end to end

## 0. Cost first

Steps 1-2 cost nothing. The live verification in step 3 uses the user's paid plan quota twice: this session plus one small nested agent prompt. Say so and get the user's confirmation before continuing to step 3. If they only want the install verified, run step 2b instead and stop.

## 1. Preflight

- `python3 --version` >= 3.10 and `git --version` works.
- Detect the host: `claude --version`, `agent status` (Cursor CLI, shows a logged-in account) or `codex --version`. Use the matching install variant below. If several exist, ask the user which host to set up, or set up the one this session is running in.
- Plugin source at `~/sylp` on `main`; `git -C ~/sylp checkout main && git -C ~/sylp pull` if present, otherwise:
  `git clone https://github.com/seanbenhur/sylp.git ~/sylp`

## 2. Install (per host)

**Claude Code** (no clone needed for the install itself):

```
claude plugin marketplace add seanbenhur/sylp
claude plugin install sylp-provenance@sylp-provenance
```

**Cursor CLI** (`agent`):

```
mkdir -p ~/.cursor/plugins/local
cp -R ~/sylp/plugins/cursor ~/.cursor/plugins/local/sylp-provenance
```

**Codex CLI**:

```
python3 ~/sylp/install.py --host codex --marketplace
```

Then in `codex`, the user runs `/plugins` to enable sylp-provenance and `/hooks` to trust its hooks.

The hooks create a per-repo capture database under `~/.sylp-provenance/repos/` the first time they record; step 5 shows how to compute its path. Captured text is stored locally and unencrypted; never use a real or private repo for the test.

## 2b. Verify without spending quota

```
cd ~/sylp && python3 -m unittest discover -s tests -v
```

All 39 checks should pass. Confirm the plugin files landed where the host expects them (marketplace plugin list for Claude Code, `~/.cursor/plugins/local/sylp-provenance/` for Cursor, the codex plugins config for Codex). Tell the user to restart their CLI so the hooks load. Stop here unless they confirmed the quota spend in step 0.

## 3. One recorded prompt

Hooks load at session start, so run a fresh nested session in a throwaway repo. Create it first: `mkdir -p ~/prov-test && git -C ~/prov-test init`. Cursor CLI:

```
cd ~/prov-test && agent -p --trust -f "Create hello.py with a function greet() that returns the string 'hello sylp', then run it with python3 and show the output."
```

`-f` auto-allows commands; acceptable only in the throwaway repo. On Claude Code or Codex, the equivalent is one small file-editing prompt in a fresh session inside the throwaway repo.

## 4. Render

```
cd ~/prov-test && python3 ~/sylp/recorder.py render ~/prov-test-history.html
```

## 5. Verify (all four)

1. `~/prov-test-history.html` exists.
2. It does not contain `No recorded agent activity yet.`
3. It contains `<svg` and `hello.py`.
4. Events and changes counts are both > 0. The per-repo database path is derived from the repo's real path:

```
export PROV_DB="$HOME/.sylp-provenance/repos/$(python3 -c "import hashlib,os; root=os.path.realpath(os.path.expanduser('~/prov-test')); print('prov-test-' + hashlib.sha1(root.encode()).hexdigest()[:12] + '.sqlite3')")"
python3 -c "import os,sqlite3; db=sqlite3.connect(os.environ['PROV_DB']); print('events:', db.execute('select count(*) from events').fetchone()[0]); print('changes:', db.execute('select count(*) from changes').fetchone()[0])"
```

## 6. Report

PASS: tell the user to open `~/prov-test-history.html`; graph on top, conversation thread below.
FAIL: name the failed check, paste exact errors, and include any `sylp-provenance capture failed:` lines verbatim.

## Fallback

If a nested session cannot run: finish the install, then have the user quit, `cd ~/prov-test`, start a fresh CLI session, send one small file-editing prompt, then render and verify as above.
