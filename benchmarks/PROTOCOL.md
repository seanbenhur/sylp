# Sylp and Codex comparison protocol

This protocol is the bar for comparative claims. Until it has been run and results are published with the versions and task data, describe Sylp's long-task focus as a design goal, not as a measured advantage.

## Comparison arms

- **Sylp:** run the task in its own worktree with `sylp run` or a saved interactive session.
- **Codex:** compare CLI interactive sessions with `codex` / `codex resume`, and unattended runs with `codex exec --json` / `codex exec resume`. Separately evaluate persistent Goals in a Codex surface that exposes them. Goals are documented as thread-scoped state; the CLI reference does not currently document `/goal`, so do not assume the command exists in a CLI build. OpenAI documents [resumable CLI conversations](https://learn.chatgpt.com/docs/codex/cli), [non-interactive Codex runs](https://learn.chatgpt.com/docs/non-interactive-mode), and [persistent Goals](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex). Record which surface, features, and version were used in every run.

Do not use a fresh Codex `exec` run as the only baseline for a durability claim. Include Codex's resume path in every interruption comparison. For long-running goals, compare ordinary resumable sessions and, as a separate arm, the persistent Goal workflow in a Codex surface that supports it. Confirm the available commands in the exact CLI version before planning a CLI run; do not assume thread commands exist in the CLI. Include any background terminal workflow available in the tested surface, and record exact commands and lifecycle events. If a feature is unavailable in the pinned version or surface, record that instead of silently substituting a weaker baseline.

Use the same repository commit, task prompt, exact model ID when both support it, and time limit for each paired run. Run both arms in the same disposable container or VM image with the same CPU, memory, filesystem, and network limits; the Sylp worktree by itself is not a security sandbox. Record the installed Sylp and Codex versions with `sylp --version` and `codex --version`, plus `git rev-parse HEAD` for each source checkout. For Codex CLI, use workspace-write and no-prompt settings. On the installed Codex CLI 0.164.0, `codex exec --help` does not list `--ask-for-approval`; use `--sandbox workspace-write -c 'approval_policy="never"'` instead. Recheck `codex exec --help` and `codex exec resume --help` for the pinned build before running. For Sylp, use `--auto-approve` only inside that disposable environment. Record each harness's tool inventory because their native tools may differ; a pass-rate difference then measures the whole harness, not just the model. Record provider and billing path. If the exact same model and billing path are unavailable in both arms, report those as separate experiments rather than a head-to-head result. Pin CLI versions and Sylp commit SHA, and compare equivalent interactive or exec modes. Run each task three times per arm in randomized order.

## Task set

The versioned task set in `cases.json` has four coding cases: a focused bug fix, a multi-file feature, a stateful TTL/LRU cache, and a CSV parser with validation rules. The same manifest also defines three durability scenarios: clarification before implementation, manual context compaction, and recovery around a long-running shell tool. Each scenario has fixed prompts or answers and a deterministic verifier outside the prepared repository. Prepare a fresh repository for every arm with `python3 benchmarks/prepare.py <case-id> <empty-destination>`. The command initializes and commits the fixture baseline, prints paths and SHA-256 hashes for its prompts/verifier, and prints scenario controls. Keep verifier files outside the agent's worktree until the run ends. Do not edit prompts or verifiers between paired runs.

Run the clarification scenario in two stages. Sylp command: `sylp run "$(cat "$prompt_file")" --repo "$repo_dir" --provider <provider> --model <model-id> --auto-approve`. Codex command: `codex exec --json --model <model-id> --cd "$repo_dir" --sandbox workspace-write -c 'approval_policy="never"' "$(cat "$prompt_file")"`. Capture the created task/thread ID; confirm Sylp reports `waiting_for_input` or Codex emitted a question, and verify the worktree diff is empty. Then resume with the exact `followup` contents: `sylp resume <task-id> --answer "$(cat "$followup")" --batch` or `codex exec resume -c 'approval_policy="never"' <thread-id> "$(cat "$followup")"`. Require the harness to ask before implementation, then run the verifier. Record the question, saved task/thread ID, and answer intervention.

Run the manual compaction scenario in one interactive session: from the prepared repository, start Sylp with `SYLP_AUTO_APPROVE=1 sylp` or start `codex -c 'sandbox_mode="workspace-write"' -c 'approval_policy="never"'`. Send `setup_prompt`, wait for the model's response, issue `compact_command`, then send `task_prompt` without repeating the setup constraints. Run the verifier against the final worktree. Record whether the compact command succeeded and whether the verifier confirms both the new task and the earlier constraints survived. The Codex CLI documents `/compact` as a session command in its [developer command reference](https://learn.chatgpt.com/docs/developer-commands); confirm it is available in the pinned version before the run.

For the long shell scenario, run its prompt with shell approval disabled only inside the disposable environment. Sylp command: `sylp run "$(cat "$prompt_file")" --repo "$repo_dir" --provider <provider> --model <model-id> --auto-approve`. Codex command: `codex exec --json --model <model-id> --cd "$repo_dir" --sandbox workspace-write -c 'approval_policy="never"' "$(cat "$prompt_file")"`. Capture the process and task/thread IDs. Interrupt after Sylp records `tool_started` for `run_command` or Codex JSONL records the `command_execution` item, and the prepared repository contains `interruption_marker`; for Sylp, also wait until the matching `tool_output` event is durable before killing the parent process. Record whether the shell process remains alive. Resume the same saved task/thread, require the agent to inspect the invocation log and output before taking action, then run the verifier. Confirm the harness trace contains exactly one invocation of the script; the verifier checks the final artifact and fails if its logged side effect was replayed.

For an unattended paired run, set `repo_dir`, `prompt_file`, `verifier_file`, and the chosen shared model ID from the prepare output, then run each arm in its own fresh fixture directory:

```sh
(cd "$repo_dir" && sylp run "$(cat "$prompt_file")" --repo "$repo_dir" \
  --provider openai --model "$model_id" --auto-approve)
(cd "$repo_dir" && codex exec --json --model "$model_id" \
  --sandbox workspace-write -c 'approval_policy="never"' "$(cat "$prompt_file")")
```

For Sylp, capture the created task ID and verify its worktree at `repo_dir/.git/sylp-worktrees/<task-id>`. For Codex, verify `repo_dir`. Keep the verifier outside the repo in both cases. If the repository uses a `.git` file or an alternate worktree directory, get Sylp's actual worktree path from `sylp status <task-id>` instead of assuming the example path.

Run the verifier against each result and save its output:

```sh
python3 "$verifier_file" "$sylp_worktree"
python3 "$verifier_file" "$repo_dir"
```

The coding set and three durability scenarios provide repeatable coverage, but still do not support a broad performance claim by themselves. Before comparing, have someone who did not author the cases review the prompts, verifiers, and expected outcomes; publish that review, the full corpus, and verifier hashes. Add further fixed tasks if a result depends on a single case.

Collect one required run record per arm, task, and repetition in JSONL. Keep optional scenario metrics `null` when they were not measured. Validate and render a per-task scorecard with:

```sh
python3 benchmarks/score.py results/*.jsonl --json-out results/summary.json
```

The scorer rejects duplicate run identifiers and malformed metrics, reports each task/arm separately, excludes null metrics from denominators, and lists failed checks. It does not treat missing measurements as passes or infer billed cost.

For the persistence track, include these reproducible interruption points:

1. Kill the harness while it is waiting for model output.
2. Kill it after a durable `tool_started` event and before `tool_finished`.
3. Kill it after all tool outputs are saved but before the next model turn.
4. Restart the same harness version and resume by task ID.

For each interruption, verify the task remains discoverable, the worktree still exists, the saved transcript is accepted by the provider, and no shell side effect is silently replayed. Record whether the model inspected state before attempting an uncertain action again.

## Scoring

Publish per-task results; do not collapse away failures behind an average.

- **Acceptance pass:** 1 only when all task-specific verifier checks pass.
- **Regression pass:** 1 only when all available baseline tests pass and no unrelated tracked files changed. If a fixture has no baseline test suite, record `null` for this metric and say so; do not treat missing tests as a pass.
- **Recovery pass:** 1 only when restart resumes the same task and reaches the same acceptance result without unsafe duplicate side effects.
- **Clarification pass:** 1 only when the required question is asked before code changes and the saved session accepts the fixed answer.
- **Compaction pass:** 1 only when the final verifier passes after the specified manual compact operation; record the command and any harness compaction event.
- **Tool recovery pass:** 1 only when the long command completes, its invocation count remains one, and the resumed task reaches the verified result.
- **Human interventions:** count approvals, answers, and manual recovery actions separately.
- **Time:** wall time from first prompt to verifier completion, including time spent paused for recovery.
- **Cost:** record provider-reported input/output tokens and billed cost when available. If either arm does not expose comparable usage, mark it unavailable; do not infer equivalence from elapsed time.

Report pass rates with the numerator and denominator, medians and ranges for time and interventions, every recovery failure, and the exact command/configuration used. Keep raw JSONL traces, Sylp task reports, diffs, verifier output, and a machine-readable result record for every run.

## Required run record

Each run record should include:

```json
{
  "task": "stable-task-id",
  "arm": "sylp-or-codex",
  "repo_commit": "full-git-sha",
  "verifier_sha256": "sha256-of-exact-verifier",
  "harness_version": "version-and-commit",
  "model": "provider/model-id",
  "billing_path": "subscription-or-api",
  "prompt_sha256": "sha256-of-exact-prompt",
  "prompt_hashes": {},
  "run_number": 1,
  "interruption": "none-or-defined-point",
  "acceptance_pass": false,
  "regression_pass": null,
  "baseline_test_command": null,
  "baseline_test_result": null,
  "unrelated_tracked_changes": [],
  "recovery_pass": null,
  "clarification_pass": null,
  "compaction_pass": null,
  "tool_recovery_pass": null,
  "tool_invocation_count": null,
  "compaction_trigger": null,
  "human_interventions": 0,
  "wall_seconds": 0,
  "input_tokens": null,
  "output_tokens": null,
  "billed_cost_usd": null,
  "artifacts": []
}
```
