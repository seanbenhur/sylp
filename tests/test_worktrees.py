from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from sylp_harness.git import prepare_worktree, refresh_clean_worktree


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


class WorktreeTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        root.mkdir()
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Sylp Test")
        git(root, "config", "user.email", "sylp-test@example.invalid")
        (root / "source.txt").write_text("committed\n", encoding="utf-8")
        git(root, "add", "source.txt")
        git(root, "commit", "-m", "baseline")

    def test_new_worktree_includes_dirty_and_untracked_checkout_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_repo(root)
            (root / "source.txt").write_text("current edit\n", encoding="utf-8")
            (root / "new.txt").write_text("untracked source\n", encoding="utf-8")

            worktree, _base = prepare_worktree(root, "snapshot001")

            self.assertEqual((worktree / "source.txt").read_text(encoding="utf-8"), "current edit\n")
            self.assertEqual((worktree / "new.txt").read_text(encoding="utf-8"), "untracked source\n")

    def test_resume_refreshes_only_pristine_task_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            self.make_repo(root)
            worktree, base = prepare_worktree(root, "refresh001")
            (root / "source.txt").write_text("new checkout state\n", encoding="utf-8")
            (root / "added.txt").write_text("new file\n", encoding="utf-8")

            self.assertTrue(refresh_clean_worktree(root, worktree, base))
            self.assertEqual((worktree / "source.txt").read_text(encoding="utf-8"), "new checkout state\n")
            self.assertEqual((worktree / "added.txt").read_text(encoding="utf-8"), "new file\n")

            (worktree / "source.txt").write_text("task changes\n", encoding="utf-8")
            (root / "source.txt").write_text("later checkout state\n", encoding="utf-8")
            self.assertFalse(refresh_clean_worktree(root, worktree, base))
            self.assertEqual((worktree / "source.txt").read_text(encoding="utf-8"), "task changes\n")


if __name__ == "__main__":
    unittest.main()
