---
name: demo
description: Run the synthetic provenance demo (throwaway repo, no quota cost)
---

Show the user what a recorded session looks like without spending any of their agent quota: the demo drives the recorder with synthetic hook payloads, not a live agent.

The demo script lives at the plugin root (two directories up from this file, at `demo.py`).

1. Run `python3 "PLUGIN_ROOT/demo.py" /tmp/sylp-demo` (delete /tmp/sylp-demo first if it exists), substituting the plugin root for PLUGIN_ROOT. This builds a throwaway repo under /tmp, records a synthetic session, and renders a history page.
2. Tell the user the rendered page path (/tmp/sylp-demo/history.html) and offer the dashboard (`serve-dashboard` skill) to browse it live.

Everything the demo produces is synthetic fixture data under /tmp; nothing touches the user's real repositories.
