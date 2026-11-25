---
description: Open the sylp provenance dashboard (localhost, read-only)
---

Start the sylp provenance web dashboard for the current repository and tell the user the URL to open.

Run exactly this in the background:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/recorder.py serve"
```

Then report the printed URL (http://127.0.0.1:8777) to the user. The server is read-only and binds to localhost. It resolves the repository database from the current working directory; if it reports no database for this repo, tell the user to run `python3 "${CLAUDE_PLUGIN_ROOT}/recorder.py init . --accept-local-capture"` first or pass `--db` with an existing database.
