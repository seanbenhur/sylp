---
name: sylp-provenance-test
description: Install the sylp provenance plugin into a throwaway repo and verify end to end that prompts, tool actions and file changes are captured and rendered into the graph view. Use when the user asks to set up, test, or verify the sylp provenance plugin.
---

# Test the sylp provenance plugin end to end

## 0. Cost first

This test uses the user's paid plan quota twice: this session plus one small nested agent prompt. Say so and get the user's confirmation before continuing.

## 1. Preflight

- `python3 --version` >= 3.10, `git --version` works, `agent status` shows a logged-in account.
- Plugin source at `~/sylp` on `main`; `git checkout main && git pull` if present, otherwise:
  `git clone https://github.com/seanbenhur/sylp.git ~/sylp`

## 2. Install

```
python3 ~/sylp/install.py --host cursor --repo ~/prov-test
```

`~/prov-test` is a throwaway repo. Captured text is stored locally and unencrypted; never use a real or private repo. The installer initializes a per-repo capture database under `~/.sylp-provenance/repos/`; note the path it prints (`initialized capture db: ...`).

## 3. One recorded prompt

Hooks load at session start, so run a fresh nested session:

```
cd ~/prov-test && agent -p --trust -f "Create hello.py with a function greet() that returns the string 'hello sylp', then run it with python3 and show the output."
```

`-f` auto-allows commands; acceptable only in the throwaway repo.

## 4. Render

```
cd ~/prov-test && python3 ~/sylp/recorder.py render ~/prov-test-history.html
```

## 5. Verify (all four)

1. `~/prov-test-history.html` exists.
2. It does not contain `No recorded agent activity yet.`
3. It contains `<svg` and `hello.py`.
4. Events and changes counts are both > 0, using the database path install printed:

```
python3 -c "import os,sqlite3; db=sqlite3.connect(os.environ['PROV_DB']); print('events:', db.execute('select count(*) from events').fetchone()[0]); print('changes:', db.execute('select count(*) from changes').fetchone()[0])"
```

## 6. Report

PASS: tell the user to open `~/prov-test-history.html`; graph on top, conversation thread below.
FAIL: name the failed check, paste exact errors, and include any `sylp-provenance capture failed:` lines verbatim.

## Fallback

If a nested session cannot run: finish the install, then have the user quit, `cd ~/prov-test && agent`, send one small file-editing prompt, then render and verify as above.
