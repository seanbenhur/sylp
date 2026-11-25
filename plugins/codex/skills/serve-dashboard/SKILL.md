---
name: serve-dashboard
description: Open the sylp provenance dashboard (localhost, read-only) for the current repository
---

Start the sylp provenance web dashboard and tell the user the URL to open.

The recorder lives at the plugin root: two directories up from this file, at `recorder.py`. Run in the background, substituting the plugin root:

```bash
python3 "PLUGIN_ROOT/recorder.py serve"
```

Report the printed URL (http://127.0.0.1:8777). The server is read-only and binds to localhost. It resolves the repository database from the current working directory; if it reports no database for this repo, tell the user to run `python3 "PLUGIN_ROOT/recorder.py init . --accept-local-capture"` first or pass `--db` with an existing database.
