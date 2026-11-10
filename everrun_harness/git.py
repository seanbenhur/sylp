from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "git command failed")
    return p.stdout.strip()


def common_dir(repo: Path) -> Path:
    git_dir = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not git_dir.is_absolute():
        git_dir = (repo / git_dir).resolve()
    return git_dir


def _snapshot_checkout(root: Path, path: Path) -> None:
    # Include staged and unstaged tracked edits in the task's starting point.
    patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=root, capture_output=True)
    if patch.returncode:
        raise RuntimeError(patch.stderr.decode(errors="replace").strip() or "Could not snapshot working tree")
    if patch.stdout:
        applied = subprocess.run(["git", "apply", "--binary", "-"], cwd=path, input=patch.stdout, capture_output=True)
        if applied.returncode:
            raise RuntimeError(applied.stderr.decode(errors="replace").strip() or "Could not apply working tree snapshot")

    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                               cwd=root, capture_output=True)
    if untracked.returncode:
        raise RuntimeError(untracked.stderr.decode(errors="replace").strip() or "Could not list untracked files")
    for raw in untracked.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="surrogateescape"))
        source, target = root / relative, path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        elif source.is_file():
            shutil.copy2(source, target)


def prepare_worktree(repo: Path, task_id: str) -> tuple[Path, str]:
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    base = git(root, "rev-parse", "HEAD")
    path = common_dir(root) / "everrun-worktrees" / task_id
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", f"everrun/{task_id}", str(path), base)
    # A task should start from what the developer currently sees, including
    # uncommitted edits. Starting from HEAD made a new worktree silently omit
    # the very source changes the user had just installed and launched.
    try:
        _snapshot_checkout(root, path)
    except Exception:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=root, capture_output=True)
        subprocess.run(["git", "branch", "-D", f"everrun/{task_id}"], cwd=root, capture_output=True)
        raise
    return path, base


def refresh_clean_worktree(repo: Path, worktree: Path, base: str) -> bool:
    """Bring a pristine, uncommitted task worktree up to the current dirty checkout."""
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    if git(root, "rev-parse", "HEAD") != base or git(worktree, "rev-parse", "HEAD") != base:
        return False
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=worktree, text=True, capture_output=True)
    if status.returncode or status.stdout.strip():
        return False
    diff = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=root, capture_output=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               cwd=root, text=True, capture_output=True)
    if diff.returncode not in (0, 1):
        raise RuntimeError(diff.stderr.decode(errors="replace").strip() or "Could not inspect checkout changes")
    if untracked.returncode:
        raise RuntimeError(untracked.stderr.strip() or "Could not inspect untracked checkout files")
    if diff.returncode == 0 and not untracked.stdout.strip():
        return False
    _snapshot_checkout(root, worktree)
    return True


def diff_summary(worktree: Path, base: str) -> str:
    p = subprocess.run(["git", "diff", "--stat", base], cwd=worktree, text=True, capture_output=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=worktree, text=True, capture_output=True)
    parts = [p.stdout.strip()]
    if untracked.stdout.strip():
        parts.append("Untracked files:\n" + untracked.stdout.strip())
    return "\n".join(x for x in parts if x) or "No changes yet."
