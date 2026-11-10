from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an isolated Sylp/Codex benchmark case")
    parser.add_argument("case", help="Case ID from benchmarks/cases.json")
    parser.add_argument("destination", type=Path, help="New, empty destination directory for this run")
    args = parser.parse_args()

    manifest = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    all_cases = [*manifest["cases"], *manifest.get("durability_scenarios", [])]
    case = next((item for item in all_cases if item["id"] == args.case), None)
    if case is None:
        raise SystemExit(f"Unknown case {args.case!r}; choose from: {', '.join(item['id'] for item in all_cases)}")
    destination = args.destination.expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise SystemExit(f"Destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    project = destination / "repo"
    shutil.copytree(ROOT / case["project"], project, dirs_exist_ok=True)
    _git(project, "init", "-b", "main")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Sylp benchmark", "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
           "GIT_COMMITTER_NAME": "Sylp benchmark", "GIT_COMMITTER_EMAIL": "benchmark@example.invalid"}
    _git(project, "add", "-A", env=env)
    _git(project, "commit", "-m", f"Benchmark baseline: {args.case}", env=env)
    baseline = _git(project, "rev-parse", "HEAD")
    prompt_paths = {
        key: ROOT / case[key]
        for key in ("prompt", "followup", "setup_prompt", "task_prompt")
        if case.get(key)
    }
    verifier = ROOT / case["verifier"]
    print(f"case={args.case}")
    print(f"repo={project}")
    print(f"verifier={verifier}")
    print(f"baseline_commit={baseline}")
    print(f"verifier_sha256={_sha256(verifier)}")
    for key, path in prompt_paths.items():
        print(f"{key}={path}")
        print(f"{key}_sha256={_sha256(path)}")
    for key in ("compact_command", "expected_pause", "interruption_marker", "expected_tool"):
        if case.get(key):
            print(f"{key}={case[key]}")
    print("Run each harness in a fresh destination. Keep the verifier outside the agent worktree until the run ends.")


if __name__ == "__main__":
    main()
