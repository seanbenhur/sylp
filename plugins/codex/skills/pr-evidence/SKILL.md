---
name: pr-evidence
description: Draft a PR provenance summary from recorded evidence (never opens a PR by itself)
---

Produce a provenance summary for the current branch so the user can attach it to a pull request.

The recorder lives at the plugin root (two directories up from this file, at `recorder.py`).

1. Ask which base ref to compare against if unknown (default: the upstream default branch).
2. Run `python3 "PLUGIN_ROOT/recorder.py pr-summary BASE HEAD` from the repository root, substituting the plugin root for PLUGIN_ROOT.
3. Show the full markdown output to the user. Point out that it contains private recorded prompt text, and offer to re-run with --no-prompts for a redacted version.
4. Files flagged "NO RECORDED PROMPT" are not necessarily human-written; say so plainly.

Opening or updating a pull request is NOT part of this skill. Only if the user explicitly asks you to create or update a specific PR after seeing the summary, use `gh pr create` (or `gh pr edit`) with the reviewed text, and confirm the exact title, body and target repo with the user immediately before running it. Never open a PR without that per-PR approval.
