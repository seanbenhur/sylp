---
description: Open the sylp provenance dashboard (localhost; read-only except for your notes)
---

Start the sylp provenance web dashboard for the current repository and tell the user the URL to open.

Run exactly this in the background, substituting this plugin's install directory (the directory containing recorder.py) for PLUGIN_ROOT:

```bash
python3 "PLUGIN_ROOT/recorder.py serve"
```

Then report the printed URL (http://127.0.0.1:8777) to the user. The server is read-only except for notes you add in the page, and binds to localhost. It resolves the repository database from the current working directory; if it reports no database for this repo, tell the user to run `python3 "PLUGIN_ROOT/recorder.py init . --accept-local-capture"` first or pass `--db` with an existing database.
