# Set up and test the sylp provenance plugin (paste this into your agent)

You are going to install a local provenance-capture plugin and prove it works end to end. Work through the steps in order. Do not skip the cost confirmation in step 0.

## 0. Cost confirmation (do this first, out loud)

This test uses the user's paid plan quota TWICE: once for this session, once for one small nested agent prompt in step 4. Tell the user that plainly and ask them to confirm. If they do not confirm, stop here.

## 1. Preflight checks

- `python3 --version` is 3.10 or newer.
- `git --version` works.
- `agent status` shows a logged-in Cursor account.
- Find the plugin source. If `~/sylp` exists, run `cd ~/sylp && git checkout main && git pull`. If `~/sylp` does not exist, clone it:
  `git clone https://github.com/seanbenhur/sylp.git ~/sylp`
- Confirm `~/sylp/install.py` exists.

## 2. Install into a throwaway repo

```
python3 ~/sylp/install.py --host cursor --repo ~/prov-test
```

This creates `~/prov-test` (a throwaway git repo), stages the plugin into `~/.cursor/plugins/local/sylp-provenance`, and initializes a per-repo capture database under `~/.sylp-provenance/repos/`. Note the exact database path it prints (`initialized capture db: ...`); you need it in step 5.

Rules for `~/prov-test`: it is disposable. Captured prompts and code are stored locally and unencrypted, so never point this at a real or private repo.

## 3. Run one small recorded prompt (nested session)

Plugin hooks load when an agent session starts, so this current session cannot record itself. Run a fresh nested session inside the throwaway repo:

```
cd ~/prov-test && agent -p --trust -f "Create hello.py with a function greet() that returns the string 'hello sylp', then run it with python3 and show the output."
```

`-f` lets the nested session run its commands without prompting; that is acceptable only because the working directory is the throwaway `~/prov-test`. If the nested run asks for login or fails on permissions, stop and report the exact output.

## 4. Render the history page

Run render from inside the recorded repo so it finds the right database:

```
cd ~/prov-test && python3 ~/sylp/recorder.py render ~/prov-test-history.html
```

## 5. Verify (all four checks)

1. `~/prov-test-history.html` exists.
2. It does NOT contain the string `No recorded agent activity yet.` (that string means the hooks never fired).
3. It contains `<svg` (the graph) and the string `hello.py` (the recorded change).
4. The database has recorded events. Use the database path the install step printed:

```
python3 -c "import os,sqlite3; db=sqlite3.connect(os.environ['PROV_DB']); print('events:', db.execute('select count(*) from events').fetchone()[0]); print('changes:', db.execute('select count(*) from changes').fetchone()[0])"
```

with `PROV_DB` set to that path. Both counts must be greater than 0.

## 6. Report

PASS: all four checks hold. Tell the user to open `~/prov-test-history.html`; they should see the graph at the top (prompt, tool window, hello.py change, and the commit if the agent committed) and the conversation thread below it.

FAIL: say exactly which check failed and paste the exact error output. Also check whether any line starting with `sylp-provenance capture failed:` appeared in the nested session's output and include it verbatim.

## Fallback if the nested run is not possible

Finish step 2, then tell the user: quit this session, run `cd ~/prov-test && agent`, send one small prompt that creates or edits a file, then run the render command from step 4 and do the checks in step 5.
